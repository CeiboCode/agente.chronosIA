from collections import defaultdict

from psycopg2.extras import RealDictCursor

from solver import _cargar_asignaciones, _cargar_slots
from restricciones_docentes import cargar_restricciones_docentes, slot_no_disponible


def _max_intervalos_no_solapados(intervalos):
    """Máximo número de intervalos compatibles usando selección por hora de fin."""
    seleccionados = 0
    fin_actual = None
    for inicio, fin in sorted(intervalos, key=lambda item: (item[1], item[0])):
        if fin_actual is None or inicio >= fin_actual:
            seleccionados += 1
            fin_actual = fin
    return seleccionados


def validar_capacidad_docentes(conn, institucion_id: int, periodo_lectivo_id: int):
    """Rechaza cargas docentes imposibles antes de ejecutar la heurística.

    Considera los perfiles usados por cada docente, sus bloques no disponibles
    y el máximo de bloques diarios configurado. El máximo de consecutivos se
    valida durante la construcción del horario porque depende del orden final.
    """
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        asignaciones = _cargar_asignaciones(cur, institucion_id, periodo_lectivo_id)
        slots = _cargar_slots(cur, institucion_id, periodo_lectivo_id)
        restricciones = cargar_restricciones_docentes(cur, institucion_id, periodo_lectivo_id)
    finally:
        cur.close()

    demanda_por_profesor = defaultdict(int)
    perfiles_por_profesor = defaultdict(set)
    meta_profesor = {}

    for asignacion in asignaciones:
        profesor_id = int(asignacion["profesor_id"])
        demanda_por_profesor[profesor_id] += int(asignacion["horas_por_semana"])
        perfiles_por_profesor[profesor_id].add(int(asignacion["perfil_horario_id"]))
        meta_profesor[profesor_id] = asignacion

    slots_por_perfil = defaultdict(list)
    for slot in slots:
        slots_por_perfil[int(slot["perfil_horario_id"])].append(slot)

    problemas = []
    for profesor_id, demanda in demanda_por_profesor.items():
        intervalos_por_dia = defaultdict(set)
        for perfil_id in perfiles_por_profesor[profesor_id]:
            for slot in slots_por_perfil.get(perfil_id, []):
                if slot_no_disponible(restricciones, profesor_id, slot):
                    continue
                dia = int(slot["dia_indice"])
                intervalos_por_dia[dia].add((slot["hora_inicio"], slot["hora_fin"]))

        regla = restricciones["reglas"].get(profesor_id) or {}
        max_dia = regla.get("max_bloques_dia")
        capacidad = 0
        for intervalos in intervalos_por_dia.values():
            capacidad_dia = _max_intervalos_no_solapados(list(intervalos))
            if max_dia is not None:
                capacidad_dia = min(capacidad_dia, int(max_dia))
            capacidad += capacidad_dia

        if demanda > capacidad:
            meta = meta_profesor[profesor_id]
            problemas.append(
                f"{meta['profesor_nombre']}: requiere {demanda} bloques semanales "
                f"y sus restricciones solo permiten {capacidad} bloques compatibles"
            )

    if problemas:
        raise ValueError(
            "La carga semanal excede la disponibilidad configurada de algunos docentes. "
            + "; ".join(problemas)
        )

    return {
        "docentes_validados": len(demanda_por_profesor),
        "problemas": 0,
    }
