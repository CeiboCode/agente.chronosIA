import random
from collections import defaultdict

from psycopg2.extras import RealDictCursor

from solver import (
    _cargar_asignaciones,
    _cargar_slots,
    _evaluar_calidad_horario,
    _validar_resultado_final,
)
from solver_heuristic import optimizar_horarios_institucion as _optimizar_base
from solver_precheck import validar_capacidad_docentes


MAX_INTERCAMBIOS = 350
MAX_SIN_MEJORA = 120
OBJETIVO_CALIDAD = 80.0


def _cargar_preferencias_periodo(cur, institucion_id, periodo_lectivo_id):
    defaults = {"peso_huecos_docentes": 40, "peso_huecos_grupos": 25, "peso_balance_diario": 20, "peso_distribucion_materias": 15}
    cur.execute(
        """SELECT peso_huecos_docentes,peso_huecos_grupos,peso_balance_diario,peso_distribucion_materias
           FROM preferencias_optimizacion_horarios
           WHERE institucion_id=%s AND periodo_lectivo_id=%s LIMIT 1""",
        (institucion_id, periodo_lectivo_id),
    )
    fila = cur.fetchone()
    if not fila:
        return defaults
    preferencias = {campo: int(fila[campo]) for campo in defaults}
    return preferencias if sum(preferencias.values()) == 100 else defaults


def _cargar_contexto_calidad(conn, institucion_id, periodo_lectivo_id):
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        asignaciones = _cargar_asignaciones(cur, institucion_id, periodo_lectivo_id)
        slots = _cargar_slots(cur, institucion_id, periodo_lectivo_id)
        preferencias = _cargar_preferencias_periodo(cur, institucion_id, periodo_lectivo_id)
        return asignaciones, slots, preferencias
    finally:
        cur.close()


def _capturar_horario(conn, institucion_id, periodo_lectivo_id):
    with conn.cursor() as cur:
        cur.execute("""SELECT asignacion_carga_id,aula_id,bloque_tiempo_id,dia_indice FROM horarios
                       WHERE institucion_id=%s AND periodo_lectivo_id=%s ORDER BY id_horario""", (institucion_id, periodo_lectivo_id))
        return [(institucion_id, periodo_lectivo_id, int(a), int(au), int(b), int(d)) for a, au, b, d in cur.fetchall()]


def _guardar_horario(conn, institucion_id, periodo_lectivo_id, horario):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM horarios WHERE institucion_id=%s AND periodo_lectivo_id=%s", (institucion_id, periodo_lectivo_id))
        if horario:
            args = b",".join(cur.mogrify("(%s,%s,%s,%s,%s,%s)", item) for item in horario)
            cur.execute(b"INSERT INTO horarios (institucion_id,periodo_lectivo_id,asignacion_carga_id,aula_id,bloque_tiempo_id,dia_indice) VALUES " + args)
    conn.commit()


def _optimizar_intercambios(institucion_id, periodo_lectivo_id, asignaciones, slots, preferencias, horario_inicial):
    asignaciones_por_id = {int(a["id_asignacion_carga"]): a for a in asignaciones}
    indices_por_grupo = defaultdict(list)
    for indice, item in enumerate(horario_inicial):
        asig = asignaciones_por_id[int(item[2])]
        indices_por_grupo[(int(asig["curso_id"]), int(asig["paralelo_id"]))].append(indice)
    grupos = [indices for indices in indices_por_grupo.values() if len(indices) >= 2]
    if not grupos:
        return horario_inicial, _evaluar_calidad_horario(asignaciones, slots, horario_inicial, preferencias), 0, 0
    mejor = list(horario_inicial)
    calidad_mejor = _evaluar_calidad_horario(asignaciones, slots, mejor, preferencias)
    puntaje_mejor = float(calidad_mejor["puntaje"]); mejoras = intentos = sin_mejora = 0
    while intentos < MAX_INTERCAMBIOS and sin_mejora < MAX_SIN_MEJORA:
        if puntaje_mejor >= OBJETIVO_CALIDAD: break
        intentos += 1
        i, j = random.sample(random.choice(grupos), 2)
        candidato = list(mejor); x, y = candidato[i], candidato[j]
        candidato[i] = (x[0],x[1],x[2],x[3],y[4],y[5]); candidato[j] = (y[0],y[1],y[2],y[3],x[4],x[5])
        try: _validar_resultado_final(asignaciones, slots, candidato)
        except ValueError: sin_mejora += 1; continue
        calidad = _evaluar_calidad_horario(asignaciones, slots, candidato, preferencias); puntaje = float(calidad["puntaje"])
        if puntaje > puntaje_mejor:
            mejor, calidad_mejor, puntaje_mejor = candidato, calidad, puntaje; mejoras += 1; sin_mejora = 0
        else: sin_mejora += 1
    return mejor, calidad_mejor, mejoras, intentos


def optimizar_horarios_institucion(institucion_id: int, periodo_lectivo_id: int, conn):
    validar_capacidad_docentes(conn, institucion_id, periodo_lectivo_id)
    resultado = _optimizar_base(institucion_id, periodo_lectivo_id, conn)
    horario_inicial = _capturar_horario(conn, institucion_id, periodo_lectivo_id)
    puntaje_inicial = float((resultado.get("calidad") or {}).get("puntaje", 0.0))
    asignaciones, slots, preferencias = _cargar_contexto_calidad(conn, institucion_id, periodo_lectivo_id)
    horario_mejor, calidad_mejor, mejoras, intentos = _optimizar_intercambios(institucion_id, periodo_lectivo_id, asignaciones, slots, preferencias, horario_inicial)
    puntaje_final = float(calidad_mejor.get("puntaje", puntaje_inicial))
    if puntaje_final > puntaje_inicial:
        _guardar_horario(conn, institucion_id, periodo_lectivo_id, horario_mejor); resultado["calidad"] = calidad_mejor
    resultado["optimizacion_calidad"] = {"metodo":"heuristica_continuidad_balance_mas_intercambios","rondas_ejecutadas":1,"puntaje_inicial":puntaje_inicial,"puntaje_final":max(puntaje_inicial,puntaje_final),"puntajes_evaluados":[puntaje_inicial,max(puntaje_inicial,puntaje_final)],"intercambios_intentados":intentos,"mejoras_aceptadas":mejoras,"objetivo_calidad":OBJETIVO_CALIDAD}
    return resultado
