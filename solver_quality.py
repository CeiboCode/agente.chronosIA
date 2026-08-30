from copy import deepcopy

from solver import optimizar_horarios_institucion as _optimizar_base


MAX_RONDAS_CALIDAD = 2
OBJETIVO_CALIDAD = 80.0


def _capturar_horario(conn, institucion_id, periodo_lectivo_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT asignacion_carga_id, aula_id, bloque_tiempo_id, dia_indice
            FROM horarios
            WHERE institucion_id = %s
              AND periodo_lectivo_id = %s
            ORDER BY id_horario
            """,
            (institucion_id, periodo_lectivo_id),
        )
        return [tuple(fila) for fila in cur.fetchall()]


def _restaurar_horario(conn, institucion_id, periodo_lectivo_id, horario):
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM horarios WHERE institucion_id = %s AND periodo_lectivo_id = %s",
            (institucion_id, periodo_lectivo_id),
        )
        if horario:
            valores = [
                (
                    institucion_id,
                    periodo_lectivo_id,
                    asignacion_carga_id,
                    aula_id,
                    bloque_tiempo_id,
                    dia_indice,
                )
                for asignacion_carga_id, aula_id, bloque_tiempo_id, dia_indice in horario
            ]
            args = b",".join(
                cur.mogrify("(%s,%s,%s,%s,%s,%s)", item)
                for item in valores
            )
            cur.execute(
                b"INSERT INTO horarios "
                b"(institucion_id,periodo_lectivo_id,asignacion_carga_id,aula_id,bloque_tiempo_id,dia_indice) VALUES "
                + args
            )
    conn.commit()


def optimizar_horarios_institucion(institucion_id: int, periodo_lectivo_id: int, conn):
    """Ejecuta varias rondas válidas y conserva físicamente el horario con mejor puntaje."""
    mejor_resultado = None
    mejor_horario = None
    puntajes = []
    rondas_ejecutadas = 0

    for ronda in range(1, MAX_RONDAS_CALIDAD + 1):
        resultado = _optimizar_base(institucion_id, periodo_lectivo_id, conn)
        rondas_ejecutadas = ronda
        calidad = resultado.get("calidad") or {}
        puntaje = float(calidad.get("puntaje", 0.0))
        puntajes.append(puntaje)

        if mejor_resultado is None or puntaje > float((mejor_resultado.get("calidad") or {}).get("puntaje", 0.0)):
            mejor_resultado = deepcopy(resultado)
            mejor_horario = _capturar_horario(conn, institucion_id, periodo_lectivo_id)

        if puntaje >= OBJETIVO_CALIDAD:
            break

    if mejor_resultado is None:
        raise ValueError("No fue posible obtener una generación válida del horario.")

    _restaurar_horario(conn, institucion_id, periodo_lectivo_id, mejor_horario)

    mejor_resultado["optimizacion_calidad"] = {
        "rondas_ejecutadas": rondas_ejecutadas,
        "puntajes_evaluados": puntajes,
        "mejor_puntaje": float((mejor_resultado.get("calidad") or {}).get("puntaje", 0.0)),
        "objetivo_calidad": OBJETIVO_CALIDAD,
    }
    return mejor_resultado
