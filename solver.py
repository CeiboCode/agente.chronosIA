from psycopg2.extras import RealDictCursor


def _buscar_aula_libre(aulas_disponibles, aulas_ocupadas, dia, bloque_id):
    """
    Devuelve la primera aula disponible para un día/bloque.
    """
    for aula_id in aulas_disponibles:
        if (aula_id, dia, bloque_id) not in aulas_ocupadas:
            return aula_id

    return None


def _validar_periodo(cur, institucion_id, periodo_lectivo_id):
    """
    Verifica que el período exista y pertenezca a la institución.
    """
    cur.execute(
        """
        SELECT
            id_periodo_lectivo,
            nombre,
            fecha_inicio,
            fecha_fin,
            activo
        FROM periodos_lectivos
        WHERE id_periodo_lectivo = %s
          AND institucion_id = %s
        LIMIT 1;
        """,
        (periodo_lectivo_id, institucion_id),
    )

    periodo = cur.fetchone()

    if not periodo:
        raise ValueError(
            "El período lectivo no existe o no pertenece a la institución."
        )

    return periodo


def optimizar_horarios_institucion(
    institucion_id: int,
    periodo_lectivo_id: int,
    conn
):
    """
    Genera el horario completo de una institución para un período lectivo.

    Reglas:

    - Nunca utiliza bloques de otro período.
    - Nunca utiliza bloques de receso.
    - Respeta días laborables.
    - Un profesor no puede estar en dos clases simultáneamente.
    - Un paralelo no puede estar en dos clases simultáneamente.
    - Un aula no puede estar en dos clases simultáneamente.
    - Respeta horas_por_semana.
    - Respeta permite_consecutivas y max_horas_consecutivas.
    - La operación es atómica.
    - Si no se puede generar todo, no se elimina el horario anterior.
    """

    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # ==========================================================
        # 1. VALIDAR PERÍODO
        # ==========================================================

        periodo = _validar_periodo(
            cur,
            institucion_id,
            periodo_lectivo_id
        )

        # ==========================================================
        # 2. OBTENER ASIGNACIONES DE CARGA
        # ==========================================================

        cur.execute(
            """
            SELECT
                ac.id_asignacion_carga,
                ac.institucion_id,
                ac.periodo_lectivo_id,
                ac.profesor_id,
                ac.materia_id,
                ac.nombre_paralelo,
                ac.horas_por_semana,

                m.nombre AS materia_nombre,
                COALESCE(m.permite_consecutivas, FALSE)
                    AS permite_consecutivas,

                COALESCE(m.max_horas_consecutivas, 1)
                    AS max_horas_consecutivas

            FROM asignaciones_carga ac

            INNER JOIN materias m
                ON m.id_materia = ac.materia_id

            WHERE ac.institucion_id = %s
              AND ac.periodo_lectivo_id = %s
              AND ac.horas_por_semana > 0

            ORDER BY
                ac.horas_por_semana DESC,
                ac.id_asignacion_carga;
            """,
            (
                institucion_id,
                periodo_lectivo_id
            ),
        )

        asignaciones = cur.fetchall()

        if not asignaciones:
            raise ValueError(
                "No existen asignaciones de carga horaria "
                "registradas para este período."
            )

        # ==========================================================
        # 3. OBTENER AULAS
        # ==========================================================

        cur.execute(
            """
            SELECT
                id_aula,
                nombre
            FROM aulas
            WHERE institucion_id = %s
            ORDER BY id_aula;
            """,
            (institucion_id,),
        )

        aulas_resultado = cur.fetchall()

        if not aulas_resultado:
            raise ValueError(
                "La institución no tiene aulas registradas."
            )

        aulas_disponibles = [
            aula["id_aula"]
            for aula in aulas_resultado
        ]

        # ==========================================================
        # 4. OBTENER BLOQUES DEL PERÍODO
        # ==========================================================

        cur.execute(
            """
            SELECT
                bt.id_bloque_tiempo,
                bt.turno_id,
                bt.periodo_lectivo_id,
                bt.orden_bloque,
                bt.hora_inicio,
                bt.hora_fin,
                bt.es_receso,

                t.nombre AS turno_nombre,

                td.dia_indice,
                td.es_dia_laboral

            FROM bloques_tiempo bt

            INNER JOIN turnos t
                ON t.id_turno = bt.turno_id

            INNER JOIN turnos_dias td
                ON td.turno_id = t.id_turno

            WHERE t.institucion_id = %s

              -- MUY IMPORTANTE:
              -- solamente bloques del período solicitado
              AND bt.periodo_lectivo_id = %s

              -- nunca usar recreos
              AND bt.es_receso = FALSE

              -- solamente días laborales
              AND td.es_dia_laboral = TRUE

            ORDER BY
                td.dia_indice,
                bt.orden_bloque,
                bt.id_bloque_tiempo;
            """,
            (
                institucion_id,
                periodo_lectivo_id
            ),
        )

        slots_disponibles = cur.fetchall()

        if not slots_disponibles:
            raise ValueError(
                "No existen bloques de tiempo laborables "
                "para el período lectivo seleccionado."
            )

        # ==========================================================
        # 5. AGRUPAR BLOQUES POR DÍA
        # ==========================================================

        dias_slots = {}

        for slot in slots_disponibles:

            dia = slot["dia_indice"]

            if dia not in dias_slots:
                dias_slots[dia] = []

            dias_slots[dia].append(slot)

        dias_ordenados = sorted(dias_slots.keys())

        # ==========================================================
        # 6. ESTRUCTURAS DE CONTROL
        # ==========================================================

        # Profesor ocupado
        #
        # (profesor_id, dia, bloque_id)
        profes_ocupados = set()

        # Paralelo ocupado
        #
        # (nombre_paralelo, dia, bloque_id)
        paralelos_ocupados = set()

        # Aula ocupada
        #
        # (aula_id, dia, bloque_id)
        aulas_ocupadas = set()

        horarios_a_insertar = []

        # Para intentar repartir una materia entre diferentes días
        dias_usados_por_asignacion = {
            asig["id_asignacion_carga"]: set()
            for asig in asignaciones
        }

        # ==========================================================
        # 7. GENERAR HORARIO EN MEMORIA
        # ==========================================================

        for asig in asignaciones:

            asignacion_id = asig["id_asignacion_carga"]

            profesor_id = asig["profesor_id"]

            paralelo = asig["nombre_paralelo"]

            horas_totales = int(
                asig["horas_por_semana"]
            )

            permite_consecutivas = bool(
                asig["permite_consecutivas"]
            )

            max_consecutivas = (
                int(asig["max_horas_consecutivas"] or 1)
                if permite_consecutivas
                else 1
            )

            # Seguridad
            if max_consecutivas < 1:
                max_consecutivas = 1

            horas_asignadas = 0

            # ======================================================
            # Intentamos distribuir las horas
            # ======================================================

            while horas_asignadas < horas_totales:

                progreso = False

                # Primero intentamos días que todavía no usamos
                dias_candidatos = sorted(
                    dias_ordenados,
                    key=lambda dia: (
                        dia in dias_usados_por_asignacion[
                            asignacion_id
                        ],
                        dia
                    )
                )

                for dia in dias_candidatos:

                    if horas_asignadas >= horas_totales:
                        break

                    bloques_del_dia = dias_slots[dia]

                    for indice, slot in enumerate(
                        bloques_del_dia
                    ):

                        if horas_asignadas >= horas_totales:
                            break

                        bloque_id = slot[
                            "id_bloque_tiempo"
                        ]

                        # ------------------------------------------
                        # Profesor
                        # ------------------------------------------

                        if (
                            profesor_id,
                            dia,
                            bloque_id
                        ) in profes_ocupados:

                            continue

                        # ------------------------------------------
                        # Paralelo
                        # ------------------------------------------

                        if (
                            paralelo,
                            dia,
                            bloque_id
                        ) in paralelos_ocupados:

                            continue

                        # ------------------------------------------
                        # Aula
                        # ------------------------------------------

                        aula = _buscar_aula_libre(
                            aulas_disponibles,
                            aulas_ocupadas,
                            dia,
                            bloque_id
                        )

                        if aula is None:
                            continue

                        # ==================================================
                        # DETERMINAR CONSECUTIVAS
                        # ==================================================

                        horas_restantes = (
                            horas_totales
                            - horas_asignadas
                        )

                        horas_a_tomar = min(
                            max_consecutivas,
                            horas_restantes
                        )

                        bloques_a_usar = [slot]

                        aulas_a_usar = [aula]

                        valido = True

                        # ------------------------------------------
                        # Verificar bloques siguientes
                        # ------------------------------------------

                        for offset in range(
                            1,
                            horas_a_tomar
                        ):

                            siguiente_indice = (
                                indice + offset
                            )

                            if (
                                siguiente_indice
                                >= len(bloques_del_dia)
                            ):

                                valido = False
                                break

                            siguiente = (
                                bloques_del_dia[
                                    siguiente_indice
                                ]
                            )

                            siguiente_id = (
                                siguiente[
                                    "id_bloque_tiempo"
                                ]
                            )

                            # Profesor
                            if (
                                profesor_id,
                                dia,
                                siguiente_id
                            ) in profes_ocupados:

                                valido = False
                                break

                            # Paralelo
                            if (
                                paralelo,
                                dia,
                                siguiente_id
                            ) in paralelos_ocupados:

                                valido = False
                                break

                            # Aula
                            siguiente_aula = (
                                _buscar_aula_libre(
                                    aulas_disponibles,
                                    aulas_ocupadas,
                                    dia,
                                    siguiente_id
                                )
                            )

                            if siguiente_aula is None:

                                valido = False
                                break

                            bloques_a_usar.append(
                                siguiente
                            )

                            aulas_a_usar.append(
                                siguiente_aula
                            )

                        if not valido:
                            continue

                        # ==================================================
                        # REGISTRAR BLOQUES
                        # ==================================================

                        for posicion, bloque in enumerate(
                            bloques_a_usar
                        ):

                            bloque_id = (
                                bloque[
                                    "id_bloque_tiempo"
                                ]
                            )

                            aula_id = (
                                aulas_a_usar[posicion]
                            )

                            # Registrar profesor
                            profes_ocupados.add(
                                (
                                    profesor_id,
                                    dia,
                                    bloque_id
                                )
                            )

                            # Registrar paralelo
                            paralelos_ocupados.add(
                                (
                                    paralelo,
                                    dia,
                                    bloque_id
                                )
                            )

                            # Registrar aula
                            aulas_ocupadas.add(
                                (
                                    aula_id,
                                    dia,
                                    bloque_id
                                )
                            )

                            horarios_a_insertar.append(
                                (
                                    institucion_id,
                                    periodo_lectivo_id,
                                    asignacion_id,
                                    aula_id,
                                    bloque_id,
                                    dia
                                )
                            )

                            horas_asignadas += 1

                        dias_usados_por_asignacion[
                            asignacion_id
                        ].add(dia)

                        progreso = True

                        break

                    if progreso:
                        break

                # ==================================================
                # NO HAY MÁS ESPACIO
                # ==================================================

                if not progreso:
                    break

            # ======================================================
            # VERIFICACIÓN CRÍTICA
            # ======================================================

            if horas_asignadas != horas_totales:

                raise ValueError(
                    "No fue posible completar la carga horaria. "
                    f"Asignación: {asignacion_id}. "
                    f"Materia: {asig['materia_nombre']}. "
                    f"Paralelo: {paralelo}. "
                    f"Requeridas: {horas_totales}. "
                    f"Asignadas: {horas_asignadas}."
                )

        # ==========================================================
        # 8. VERIFICAR QUE TENEMOS ALGO QUE INSERTAR
        # ==========================================================

        if not horarios_a_insertar:

            raise ValueError(
                "No se pudo generar ningún horario."
            )

        # ==========================================================
        # 9. AHORA SÍ ELIMINAMOS EL HORARIO ANTERIOR
        # ==========================================================

        cur.execute(
            """
            DELETE FROM horarios
            WHERE institucion_id = %s
              AND periodo_lectivo_id = %s;
            """,
            (
                institucion_id,
                periodo_lectivo_id
            ),
        )

        # ==========================================================
        # 10. INSERTAR NUEVO HORARIO
        # ==========================================================

        args_str = b",".join(
            cur.mogrify(
                """
                (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                """,
                item
            )
            for item in horarios_a_insertar
        )

        cur.execute(
            b"""
            INSERT INTO horarios (
                institucion_id,
                periodo_lectivo_id,
                asignacion_carga_id,
                aula_id,
                bloque_tiempo_id,
                dia_indice
            )
            VALUES
            """
            + args_str
        )

        # ==========================================================
        # 11. COMMIT
        # ==========================================================

        conn.commit()

        return {
            "success": True,
            "institucion_id": institucion_id,
            "periodo_lectivo_id": periodo_lectivo_id,
            "periodo_nombre": periodo["nombre"],
            "horarios_creados": len(
                horarios_a_insertar
            )
        }

    except Exception:
        # ==========================================================
        # SI FALLA CUALQUIER COSA:
        #
        # - INSERT
        # - DELETE
        # - VALIDACIÓN
        # - GENERACIÓN
        #
        # TODO VUELVE ATRÁS.
        # ==========================================================

        conn.rollback()

        raise

    finally:
        cur.close()