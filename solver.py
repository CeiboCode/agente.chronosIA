from psycopg2.extras import RealDictCursor
import random


def _buscar_aula_libre(
    aulas_disponibles,
    aulas_ocupadas,
    dia,
    bloque_id,
    aula_preferida=None,
):
    """
    Busca un aula libre.

    Prioridad:
    1. Aula preferida.
    2. Cualquier otra aula disponible.
    """

    if (
        aula_preferida is not None
        and (aula_preferida, dia, bloque_id) not in aulas_ocupadas
    ):
        return aula_preferida

    for aula_id in aulas_disponibles:
        if aula_id == aula_preferida:
            continue

        if (aula_id, dia, bloque_id) not in aulas_ocupadas:
            return aula_id

    return None


def _validar_periodo(
    cur,
    institucion_id,
    periodo_lectivo_id,
):
    """
    Verifica que el período exista y pertenezca
    a la institución.
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
        (
            periodo_lectivo_id,
            institucion_id,
        ),
    )

    periodo = cur.fetchone()

    if not periodo:
        raise ValueError(
            "El período lectivo no existe o no pertenece "
            "a la institución."
        )

    return periodo


def optimizar_horarios_institucion(
    institucion_id: int,
    periodo_lectivo_id: int,
    conn,
):
    """
    Genera el horario completo de una institución.

    Características:

    - Solo usa bloques del período seleccionado.
    - No utiliza recesos.
    - Solo utiliza días laborables.
    - Un profesor no puede tener dos clases simultáneas.
    - Un paralelo no puede tener dos clases simultáneas.
    - Un aula no puede tener dos clases simultáneas.
    - Intenta mantener aula fija por paralelo.
    - Respeta horas_por_semana.
    - Respeta permite_consecutivas.
    - Respeta max_horas_consecutivas.
    - Distribuye las materias entre diferentes días.
    - Distribuye las materias entre diferentes horas.
    - Evita repetir innecesariamente la misma hora.
    - Utiliza múltiples intentos para evitar bloqueos
      provocados por decisiones greedy.
    - Solo guarda el resultado cuando todas las cargas
      horarias están completas.
    - La operación es atómica.
    """

    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:

        # ==========================================================
        # 1. VALIDAR PERÍODO
        # ==========================================================

        periodo = _validar_periodo(
            cur,
            institucion_id,
            periodo_lectivo_id,
        )

        # ==========================================================
        # 2. OBTENER ASIGNACIONES
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

                COALESCE(
                    m.permite_consecutivas,
                    FALSE
                ) AS permite_consecutivas,

                COALESCE(
                    m.max_horas_consecutivas,
                    1
                ) AS max_horas_consecutivas

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
                periodo_lectivo_id,
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
        # 4. AULA PREFERIDA POR PARALELO
        # ==========================================================

        paralelos = []

        for asig in asignaciones:

            paralelo = asig["nombre_paralelo"]

            if paralelo not in paralelos:
                paralelos.append(paralelo)

        aulas_preferidas_por_paralelo = {}

        for indice, paralelo in enumerate(paralelos):

            aulas_preferidas_por_paralelo[
                paralelo
            ] = aulas_disponibles[
                indice % len(aulas_disponibles)
            ]

        # ==========================================================
        # 5. OBTENER BLOQUES
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
              AND bt.periodo_lectivo_id = %s
              AND bt.es_receso = FALSE
              AND td.es_dia_laboral = TRUE

            ORDER BY
                td.dia_indice,
                bt.orden_bloque,
                bt.id_bloque_tiempo;
            """,
            (
                institucion_id,
                periodo_lectivo_id,
            ),
        )

        slots = cur.fetchall()

        if not slots:
            raise ValueError(
                "No existen bloques de tiempo laborables "
                "para el período lectivo seleccionado."
            )

        # ==========================================================
        # 6. AGRUPAR POR DÍA
        # ==========================================================

        dias_slots = {}

        for slot in slots:

            dia = slot["dia_indice"]

            if dia not in dias_slots:
                dias_slots[dia] = []

            dias_slots[dia].append(slot)

        dias_ordenados = sorted(
            dias_slots.keys()
        )

        # ==========================================================
        # 7. ÍNDICE GLOBAL DE BLOQUES
        # ==========================================================

        #
        # Esto es importante.
        #
        # El bloque 0 de lunes y el bloque 0 de martes
        # representan la misma posición horaria.
        #
        # Por ejemplo:
        #
        # 0 = 07:00
        # 1 = 07:45
        # 2 = 08:30
        #
        # Esto permite distribuir una materia entre distintas
        # horas sin confundir los días.
        #

        posiciones_horarias = {}

        for dia in dias_ordenados:

            for indice, slot in enumerate(
                dias_slots[dia]
            ):

                bloque_id = slot[
                    "id_bloque_tiempo"
                ]

                posiciones_horarias[
                    bloque_id
                ] = indice

        # ==========================================================
        # 8. CANDIDATOS GLOBALES
        # ==========================================================

        todos_los_slots = []

        for dia in dias_ordenados:

            for indice, slot in enumerate(
                dias_slots[dia]
            ):

                todos_los_slots.append(
                    {
                        "dia": dia,
                        "indice": indice,
                        "slot": slot,
                    }
                )

        # ==========================================================
        # 9. FUNCIÓN INTERNA PARA GENERAR UN INTENTO
        # ==========================================================

        def generar_intento():

            profes_ocupados = set()

            paralelos_ocupados = set()

            aulas_ocupadas = set()

            horarios = []

            dias_usados = {
                asig["id_asignacion_carga"]: set()
                for asig in asignaciones
            }

            horas_usadas = {
                asig["id_asignacion_carga"]: {}
                for asig in asignaciones
            }

            # ------------------------------------------------------
            # Copiamos las asignaciones.
            #
            # Las más difíciles van primero.
            # ------------------------------------------------------

            asignaciones_ordenadas = list(
                asignaciones
            )

            random.shuffle(
                asignaciones_ordenadas
            )

            asignaciones_ordenadas.sort(
                key=lambda x: (
                    -int(x["horas_por_semana"]),
                    x["id_asignacion_carga"],
                )
            )

            # ------------------------------------------------------
            # Procesar cada asignación
            # ------------------------------------------------------

            for asig in asignaciones_ordenadas:

                asignacion_id = (
                    asig["id_asignacion_carga"]
                )

                profesor_id = (
                    asig["profesor_id"]
                )

                paralelo = (
                    asig["nombre_paralelo"]
                )

                horas_totales = int(
                    asig["horas_por_semana"]
                )

                permite_consecutivas = bool(
                    asig["permite_consecutivas"]
                )

                max_consecutivas = int(
                    asig["max_horas_consecutivas"]
                    or 1
                )

                if not permite_consecutivas:
                    max_consecutivas = 1

                if max_consecutivas < 1:
                    max_consecutivas = 1

                aula_preferida = (
                    aulas_preferidas_por_paralelo[
                        paralelo
                    ]
                )

                horas_asignadas = 0

                # --------------------------------------------------
                # Guardamos los bloques de ESTA asignación.
                # --------------------------------------------------

                bloques_asignacion = []

                # --------------------------------------------------
                # Mientras falten horas
                # --------------------------------------------------

                while (
                    horas_asignadas
                    < horas_totales
                ):

                    candidatos = []

                    # ==============================================
                    # BUSCAR CANDIDATOS
                    # ==============================================

                    for candidato in todos_los_slots:

                        dia = candidato[
                            "dia"
                        ]

                        indice = candidato[
                            "indice"
                        ]

                        slot = candidato[
                            "slot"
                        ]

                        bloque_id = slot[
                            "id_bloque_tiempo"
                        ]

                        # ------------------------------------------
                        # PROFESOR
                        # ------------------------------------------

                        if (
                            profesor_id,
                            dia,
                            bloque_id,
                        ) in profes_ocupados:

                            continue

                        # ------------------------------------------
                        # PARALELO
                        # ------------------------------------------

                        if (
                            paralelo,
                            dia,
                            bloque_id,
                        ) in paralelos_ocupados:

                            continue

                        # ------------------------------------------
                        # AULA
                        # ------------------------------------------

                        aula = _buscar_aula_libre(
                            aulas_disponibles,
                            aulas_ocupadas,
                            dia,
                            bloque_id,
                            aula_preferida,
                        )

                        if aula is None:
                            continue

                        # ------------------------------------------
                        # CONSECUTIVAS
                        # ------------------------------------------

                        consecutivas_actuales = 0

                        if bloques_asignacion:

                            ultimo_dia, ultimo_indice = (
                                bloques_asignacion[-1]
                            )

                            if (
                                ultimo_dia == dia
                                and indice
                                == ultimo_indice + 1
                            ):

                                consecutivas_actuales = 1

                                for (
                                    dia_anterior,
                                    indice_anterior,
                                ) in reversed(
                                    bloques_asignacion
                                ):

                                    if (
                                        dia_anterior
                                        != dia
                                    ):
                                        break

                                    if (
                                        indice_anterior
                                        == indice
                                        - consecutivas_actuales
                                    ):
                                        consecutivas_actuales += 1
                                    else:
                                        break

                        if (
                            not permite_consecutivas
                            and bloques_asignacion
                        ):

                            ultimo_dia, ultimo_indice = (
                                bloques_asignacion[-1]
                            )

                            if (
                                ultimo_dia == dia
                                and indice
                                == ultimo_indice + 1
                            ):
                                continue

                        if (
                            permite_consecutivas
                            and consecutivas_actuales
                            >= max_consecutivas
                        ):
                            continue

                        # ------------------------------------------
                        # DÍA UTILIZADO
                        # ------------------------------------------

                        dia_ya_usado = (
                            dia
                            in dias_usados[
                                asignacion_id
                            ]
                        )

                        # ------------------------------------------
                        # HORA UTILIZADA
                        # ------------------------------------------

                        cantidad_hora = (
                            horas_usadas[
                                asignacion_id
                            ].get(
                                indice,
                                0,
                            )
                        )

                        # ------------------------------------------
                        # CALCULAR PUNTUACIÓN
                        # ------------------------------------------

                        puntuacion = 0

                        # Preferir días nuevos.
                        if not dia_ya_usado:
                            puntuacion += 500
                        else:
                            puntuacion -= 100

                        # Preferir horas nuevas.
                        if cantidad_hora == 0:
                            puntuacion += 400
                        else:
                            puntuacion -= (
                                cantidad_hora * 250
                            )

                        # Aula preferida.
                        if aula == aula_preferida:
                            puntuacion += 100

                        # Intentar distribuir la hora.
                        puntuacion -= (
                            indice * 5
                        )

                        # Si permite consecutivas,
                        # favorecemos ligeramente continuidad.
                        if permite_consecutivas:

                            if (
                                bloques_asignacion
                            ):

                                ultimo_dia, ultimo_indice = (
                                    bloques_asignacion[-1]
                                )

                                if (
                                    ultimo_dia == dia
                                    and indice
                                    == ultimo_indice + 1
                                ):
                                    puntuacion += 80

                        # Si NO permite consecutivas,
                        # penalizar estar cerca.
                        else:

                            if bloques_asignacion:

                                for (
                                    dia_anterior,
                                    indice_anterior,
                                ) in bloques_asignacion:

                                    if (
                                        dia_anterior
                                        == dia
                                    ):

                                        distancia = abs(
                                            indice
                                            - indice_anterior
                                        )

                                        if distancia == 1:
                                            puntuacion -= 300

                                        elif distancia == 2:
                                            puntuacion -= 50

                        # Pequeña aleatoriedad para que
                        # los intentos no sean idénticos.
                        puntuacion += random.randint(
                            0,
                            100,
                        )

                        candidatos.append(
                            (
                                puntuacion,
                                dia,
                                indice,
                                slot,
                                aula,
                            )
                        )

                    # ==============================================
                    # NO EXISTE CANDIDATO
                    # ==============================================

                    if not candidatos:
                        return None

                    # ==============================================
                    # ORDENAR
                    # ==============================================

                    candidatos.sort(
                        key=lambda x: -x[0]
                    )

                    # ==============================================
                    # TOMAR UNO DE LOS MEJORES
                    # ==============================================

                    cantidad_mejores = min(
                        5,
                        len(candidatos),
                    )

                    candidato = random.choice(
                        candidatos[
                            :cantidad_mejores
                        ]
                    )

                    (
                        puntuacion,
                        dia,
                        indice,
                        slot,
                        aula,
                    ) = candidato

                    bloque_id = slot[
                        "id_bloque_tiempo"
                    ]

                    # ==============================================
                    # REGISTRAR
                    # ==============================================

                    profes_ocupados.add(
                        (
                            profesor_id,
                            dia,
                            bloque_id,
                        )
                    )

                    paralelos_ocupados.add(
                        (
                            paralelo,
                            dia,
                            bloque_id,
                        )
                    )

                    aulas_ocupadas.add(
                        (
                            aula,
                            dia,
                            bloque_id,
                        )
                    )

                    horarios.append(
                        (
                            institucion_id,
                            periodo_lectivo_id,
                            asignacion_id,
                            aula,
                            bloque_id,
                            dia,
                        )
                    )

                    bloques_asignacion.append(
                        (
                            dia,
                            indice,
                        )
                    )

                    dias_usados[
                        asignacion_id
                    ].add(dia)

                    horas_usadas[
                        asignacion_id
                    ][
                        indice
                    ] = (
                        horas_usadas[
                            asignacion_id
                        ].get(
                            indice,
                            0,
                        )
                        + 1
                    )

                    horas_asignadas += 1

                # --------------------------------------------------
                # Verificación de asignación
                # --------------------------------------------------

                if (
                    horas_asignadas
                    != horas_totales
                ):
                    return None

            return {
                "horarios": horarios,
                "profes_ocupados": profes_ocupados,
                "paralelos_ocupados": paralelos_ocupados,
                "aulas_ocupadas": aulas_ocupadas,
            }

        # ==========================================================
        # 10. MÚLTIPLES INTENTOS
        # ==========================================================

        #
        # Esto es lo importante para tu problema.
        #
        # No confiamos en una sola generación.
        #
        # Probamos distintas combinaciones.
        #

        resultado_final = None

        max_intentos = 300

        for intento in range(
            1,
            max_intentos + 1,
        ):

            resultado = generar_intento()

            if resultado is not None:

                resultado_final = resultado

                break

        # ==========================================================
        # 11. SI NO SE PUDO GENERAR
        # ==========================================================

        if resultado_final is None:

            # ------------------------------------------------------
            # Obtener información útil para diagnóstico.
            # ------------------------------------------------------

            detalles = []

            for asig in asignaciones:

                detalles.append(
                    f"Asignación "
                    f"{asig['id_asignacion_carga']}: "
                    f"{asig['materia_nombre']} - "
                    f"{asig['nombre_paralelo']} - "
                    f"{int(asig['horas_por_semana'])} horas"
                )

            detalle_texto = "; ".join(
                detalles
            )

            raise ValueError(
                "No fue posible generar un horario "
                "completo después de "
                f"{max_intentos} intentos. "
                "Es posible que las restricciones "
                "actuales sean incompatibles. "
                f"Asignaciones: {detalle_texto}"
            )

        # ==========================================================
        # 12. EXTRAER RESULTADO
        # ==========================================================

        horarios_a_insertar = (
            resultado_final[
                "horarios"
            ]
        )

        if not horarios_a_insertar:

            raise ValueError(
                "No se pudo generar ningún horario."
            )

        # ==========================================================
        # 13. VALIDACIÓN FINAL DE HORAS
        # ==========================================================

        horas_generadas = {}

        for horario in horarios_a_insertar:

            asignacion_id = horario[2]

            horas_generadas[
                asignacion_id
            ] = (
                horas_generadas.get(
                    asignacion_id,
                    0,
                )
                + 1
            )

        for asig in asignaciones:

            asignacion_id = (
                asig["id_asignacion_carga"]
            )

            requeridas = int(
                asig["horas_por_semana"]
            )

            generadas = horas_generadas.get(
                asignacion_id,
                0,
            )

            if generadas != requeridas:

                raise ValueError(
                    "La validación final del horario "
                    "falló. "
                    f"Asignación: {asignacion_id}. "
                    f"Materia: {asig['materia_nombre']}. "
                    f"Paralelo: {asig['nombre_paralelo']}. "
                    f"Requeridas: {requeridas}. "
                    f"Generadas: {generadas}."
                )

        # ==========================================================
        # 14. ELIMINAR HORARIO ANTERIOR
        # ==========================================================

        cur.execute(
            """
            DELETE FROM horarios
            WHERE institucion_id = %s
              AND periodo_lectivo_id = %s;
            """,
            (
                institucion_id,
                periodo_lectivo_id,
            ),
        )

        # ==========================================================
        # 15. INSERTAR NUEVO HORARIO
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
                item,
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
        # 16. COMMIT
        # ==========================================================

        conn.commit()

        return {
            "success": True,
            "institucion_id": institucion_id,
            "periodo_lectivo_id": periodo_lectivo_id,
            "periodo_nombre": periodo["nombre"],
            "horarios_creados": len(
                horarios_a_insertar
            ),
            "intentos_utilizados": intento,
            "aulas_preferidas": (
                aulas_preferidas_por_paralelo
            ),
        }

    except Exception:

        conn.rollback()

        raise

    finally:

        cur.close()