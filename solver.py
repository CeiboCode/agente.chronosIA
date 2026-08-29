from collections import defaultdict
from psycopg2.extras import RealDictCursor
import random


def _validar_periodo(cur, institucion_id, periodo_lectivo_id):
    cur.execute(
        """
        SELECT id_periodo_lectivo, nombre, fecha_inicio, fecha_fin, activo
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


def _cargar_asignaciones(cur, institucion_id, periodo_lectivo_id):
    cur.execute(
        """
        SELECT
            ac.id_asignacion_carga,
            ac.institucion_id,
            ac.periodo_lectivo_id,
            ac.profesor_id,
            ac.aula_id,
            ac.horas_por_semana,
            p.nombre AS profesor_nombre,
            a.curso_id,
            a.paralelo_id,
            a.materia_id,
            c.nombre AS curso_nombre,
            pa.nombre AS paralelo_nombre,
            m.nombre AS materia_nombre,
            COALESCE(m.permite_consecutivas, FALSE) AS permite_consecutivas,
            COALESCE(m.max_horas_consecutivas, 1) AS max_horas_consecutivas,
            cpt.turno_id,
            t.nombre AS turno_nombre
        FROM asignaciones_carga ac
        INNER JOIN profesores p
            ON p.id_profesor = ac.profesor_id
           AND p.institucion_id = ac.institucion_id
        INNER JOIN aulas a
            ON a.id_aula = ac.aula_id
           AND a.institucion_id = ac.institucion_id
        INNER JOIN cursos c
            ON c.id_curso = a.curso_id
           AND c.institucion_id = ac.institucion_id
        INNER JOIN paralelos pa
            ON pa.id_paralelo = a.paralelo_id
           AND pa.curso_id = a.curso_id
        INNER JOIN materias m
            ON m.id_materia = a.materia_id
           AND m.institucion_id = ac.institucion_id
        LEFT JOIN curso_paralelo_turnos cpt
            ON cpt.institucion_id = ac.institucion_id
           AND cpt.periodo_lectivo_id = ac.periodo_lectivo_id
           AND cpt.curso_id = a.curso_id
           AND cpt.paralelo_id = a.paralelo_id
        LEFT JOIN turnos t
            ON t.id_turno = cpt.turno_id
           AND t.institucion_id = ac.institucion_id
        WHERE ac.institucion_id = %s
          AND ac.periodo_lectivo_id = %s
          AND ac.horas_por_semana > 0
        ORDER BY ac.horas_por_semana DESC, ac.id_asignacion_carga;
        """,
        (institucion_id, periodo_lectivo_id),
    )
    asignaciones = cur.fetchall()

    if not asignaciones:
        raise ValueError(
            "No existen asignaciones de carga horaria registradas para este período."
        )

    sin_turno = [a for a in asignaciones if not a["turno_id"]]
    if sin_turno:
        faltantes = sorted(
            {
                f"{a['curso_nombre']} {a['paralelo_nombre']}"
                for a in sin_turno
            }
        )
        raise ValueError(
            "Hay cursos/paralelos sin turno asignado para este período: "
            + ", ".join(faltantes)
            + "."
        )

    return asignaciones


def _cargar_slots(cur, institucion_id, periodo_lectivo_id):
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
        ORDER BY bt.turno_id, td.dia_indice, bt.orden_bloque, bt.id_bloque_tiempo;
        """,
        (institucion_id, periodo_lectivo_id),
    )
    slots = cur.fetchall()
    if not slots:
        raise ValueError(
            "No existen bloques de tiempo laborables para el período seleccionado."
        )
    return slots


def _validar_capacidad(asignaciones, slots_por_turno):
    demanda_por_grupo = defaultdict(int)
    grupos_meta = {}

    for asig in asignaciones:
        grupo = (asig["curso_id"], asig["paralelo_id"])
        demanda_por_grupo[grupo] += int(asig["horas_por_semana"])
        grupos_meta[grupo] = asig

    problemas = []
    for grupo, demanda in demanda_por_grupo.items():
        meta = grupos_meta[grupo]
        capacidad = len(slots_por_turno.get(meta["turno_id"], []))
        if demanda > capacidad:
            problemas.append(
                f"{meta['curso_nombre']} {meta['paralelo_nombre']}: "
                f"requiere {demanda} bloques y el turno {meta['turno_nombre']} ofrece {capacidad}"
            )

    if problemas:
        raise ValueError(
            "La carga semanal excede la capacidad disponible de algunos cursos/paralelos. "
            + "; ".join(problemas)
        )


def _racha_consecutiva(bloques_asignacion, dia, orden_bloque):
    ordenes = sorted(
        orden for dia_usado, orden in bloques_asignacion if dia_usado == dia
    )
    consecutivas = 1
    anterior = orden_bloque - 1
    while anterior in ordenes:
        consecutivas += 1
        anterior -= 1
    return consecutivas


def optimizar_horarios_institucion(
    institucion_id: int,
    periodo_lectivo_id: int,
    conn,
):
    """
    Genera el horario usando el modelo académico actual:

    - aula = curso + paralelo + materia.
    - cada curso/paralelo usa únicamente los bloques de su turno.
    - no usa recesos ni días no laborables.
    - un profesor no puede estar en dos clases al mismo tiempo.
    - un curso/paralelo no puede tener dos materias al mismo tiempo.
    - cada asignación cumple exactamente horas_por_semana.
    - respeta consecutivas configuradas por materia.
    - distribuye materias entre días y posiciones horarias.
    - reemplaza el horario anterior solo si encuentra una solución completa.
    """

    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        periodo = _validar_periodo(cur, institucion_id, periodo_lectivo_id)
        asignaciones = _cargar_asignaciones(cur, institucion_id, periodo_lectivo_id)
        slots = _cargar_slots(cur, institucion_id, periodo_lectivo_id)

        slots_por_turno = defaultdict(list)
        for slot in slots:
            slots_por_turno[slot["turno_id"]].append(slot)

        turnos_sin_bloques = sorted(
            {
                asig["turno_nombre"]
                for asig in asignaciones
                if not slots_por_turno.get(asig["turno_id"])
            }
        )
        if turnos_sin_bloques:
            raise ValueError(
                "Los siguientes turnos no tienen bloques generados para este período: "
                + ", ".join(turnos_sin_bloques)
                + "."
            )

        _validar_capacidad(asignaciones, slots_por_turno)

        def generar_intento():
            profesor_ocupado = set()
            grupo_ocupado = set()
            horarios = []
            dias_usados = defaultdict(set)
            posiciones_usadas = defaultdict(lambda: defaultdict(int))
            bloques_por_asignacion = defaultdict(list)

            asignaciones_ordenadas = list(asignaciones)
            random.shuffle(asignaciones_ordenadas)
            asignaciones_ordenadas.sort(
                key=lambda a: (
                    -int(a["horas_por_semana"]),
                    len(slots_por_turno[a["turno_id"]]),
                    a["id_asignacion_carga"],
                )
            )

            for asig in asignaciones_ordenadas:
                asignacion_id = asig["id_asignacion_carga"]
                profesor_id = asig["profesor_id"]
                grupo = (asig["curso_id"], asig["paralelo_id"])
                horas_totales = int(asig["horas_por_semana"])
                permite_consecutivas = bool(asig["permite_consecutivas"])
                max_consecutivas = max(1, int(asig["max_horas_consecutivas"] or 1))
                if not permite_consecutivas:
                    max_consecutivas = 1

                for _ in range(horas_totales):
                    candidatos = []

                    for slot in slots_por_turno[asig["turno_id"]]:
                        dia = int(slot["dia_indice"])
                        bloque_id = slot["id_bloque_tiempo"]
                        orden_bloque = int(slot["orden_bloque"])

                        if (profesor_id, dia, bloque_id) in profesor_ocupado:
                            continue
                        if (grupo, dia, bloque_id) in grupo_ocupado:
                            continue

                        bloques_asignacion = bloques_por_asignacion[asignacion_id]
                        racha = _racha_consecutiva(
                            bloques_asignacion,
                            dia,
                            orden_bloque,
                        )

                        if not permite_consecutivas:
                            if any(
                                dia_anterior == dia
                                and abs(orden_anterior - orden_bloque) == 1
                                for dia_anterior, orden_anterior in bloques_asignacion
                            ):
                                continue
                        elif racha > max_consecutivas:
                            continue

                        puntuacion = 0

                        if dia not in dias_usados[asignacion_id]:
                            puntuacion += 500
                        else:
                            puntuacion -= 100

                        repeticiones_posicion = posiciones_usadas[asignacion_id][orden_bloque]
                        if repeticiones_posicion == 0:
                            puntuacion += 300
                        else:
                            puntuacion -= repeticiones_posicion * 180

                        clases_grupo_dia = sum(
                            1
                            for g, d, _ in grupo_ocupado
                            if g == grupo and d == dia
                        )
                        puntuacion -= clases_grupo_dia * 20

                        if permite_consecutivas and any(
                            dia_anterior == dia
                            and orden_anterior == orden_bloque - 1
                            for dia_anterior, orden_anterior in bloques_asignacion
                        ):
                            puntuacion += 60

                        puntuacion += random.randint(0, 100)
                        candidatos.append((puntuacion, slot))

                    if not candidatos:
                        return None

                    candidatos.sort(key=lambda item: -item[0])
                    _, slot = random.choice(candidatos[: min(5, len(candidatos))])

                    dia = int(slot["dia_indice"])
                    bloque_id = slot["id_bloque_tiempo"]
                    orden_bloque = int(slot["orden_bloque"])

                    profesor_ocupado.add((profesor_id, dia, bloque_id))
                    grupo_ocupado.add((grupo, dia, bloque_id))
                    dias_usados[asignacion_id].add(dia)
                    posiciones_usadas[asignacion_id][orden_bloque] += 1
                    bloques_por_asignacion[asignacion_id].append((dia, orden_bloque))

                    horarios.append(
                        (
                            institucion_id,
                            periodo_lectivo_id,
                            asignacion_id,
                            asig["aula_id"],
                            bloque_id,
                            dia,
                        )
                    )

            return horarios

        resultado_final = None
        max_intentos = 500
        intento_utilizado = 0

        for intento in range(1, max_intentos + 1):
            resultado = generar_intento()
            if resultado is not None:
                resultado_final = resultado
                intento_utilizado = intento
                break

        if resultado_final is None:
            detalles = [
                (
                    f"{a['curso_nombre']} {a['paralelo_nombre']} · "
                    f"{a['materia_nombre']} · {a['profesor_nombre']} · "
                    f"{int(a['horas_por_semana'])} bloques"
                )
                for a in asignaciones
            ]
            raise ValueError(
                "No fue posible generar un horario completo después de "
                f"{max_intentos} intentos. Revisa la carga, los turnos o la disponibilidad "
                "de bloques. Asignaciones: "
                + "; ".join(detalles)
            )

        horas_generadas = defaultdict(int)
        for horario in resultado_final:
            horas_generadas[horario[2]] += 1

        for asig in asignaciones:
            asignacion_id = asig["id_asignacion_carga"]
            requeridas = int(asig["horas_por_semana"])
            generadas = horas_generadas[asignacion_id]
            if generadas != requeridas:
                raise ValueError(
                    "La validación final del horario falló. "
                    f"Asignación {asignacion_id}: requeridas {requeridas}, generadas {generadas}."
                )

        cur.execute(
            """
            DELETE FROM horarios
            WHERE institucion_id = %s
              AND periodo_lectivo_id = %s;
            """,
            (institucion_id, periodo_lectivo_id),
        )

        args_str = b",".join(
            cur.mogrify(
                "(%s, %s, %s, %s, %s, %s)",
                item,
            )
            for item in resultado_final
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
            ) VALUES
            """
            + args_str
        )

        conn.commit()

        grupos = {
            (a["curso_id"], a["paralelo_id"])
            for a in asignaciones
        }
        profesores = {a["profesor_id"] for a in asignaciones}

        return {
            "success": True,
            "institucion_id": institucion_id,
            "periodo_lectivo_id": periodo_lectivo_id,
            "periodo_nombre": periodo["nombre"],
            "horarios_creados": len(resultado_final),
            "intentos_utilizados": intento_utilizado,
            "cursos_paralelos": len(grupos),
            "profesores": len(profesores),
            "asignaciones": len(asignaciones),
        }

    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
