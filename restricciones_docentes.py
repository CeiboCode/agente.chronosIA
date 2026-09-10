from collections import defaultdict


def cargar_restricciones_docentes(cur, institucion_id: int, periodo_lectivo_id: int):
    cur.execute(
        """
        SELECT profesor_id, max_bloques_dia, max_bloques_consecutivos,
               evitar_primera_hora, evitar_ultima_hora, evitar_huecos
        FROM profesor_restricciones
        WHERE institucion_id=%s AND periodo_lectivo_id=%s
        """,
        (institucion_id, periodo_lectivo_id),
    )
    reglas = {int(row["profesor_id"]): row for row in cur.fetchall()}

    cur.execute(
        """
        SELECT profesor_id, perfil_horario_id, dia_indice, hora_inicio, hora_fin
        FROM profesor_bloques_no_disponibles
        WHERE institucion_id=%s AND periodo_lectivo_id=%s
        """,
        (institucion_id, periodo_lectivo_id),
    )
    no_disponibles = {
        (
            int(row["profesor_id"]),
            int(row["perfil_horario_id"]),
            int(row["dia_indice"]),
            row["hora_inicio"],
            row["hora_fin"],
        )
        for row in cur.fetchall()
    }
    return {"reglas": reglas, "no_disponibles": no_disponibles}


def slot_no_disponible(restricciones, profesor_id: int, slot) -> bool:
    return (
        int(profesor_id),
        int(slot["perfil_horario_id"]),
        int(slot["dia_indice"]),
        slot["hora_inicio"],
        slot["hora_fin"],
    ) in restricciones["no_disponibles"]


def _racha_maxima(slots) -> int:
    if not slots:
        return 0
    por_dia = defaultdict(list)
    for slot in slots:
        por_dia[int(slot["dia_indice"])].append(slot)

    maxima = 0
    for items in por_dia.values():
        ordenados = sorted(items, key=lambda s: (s["hora_inicio"], s["hora_fin"]))
        racha = 1
        maxima = max(maxima, racha)
        for indice in range(1, len(ordenados)):
            if ordenados[indice - 1]["hora_fin"] == ordenados[indice]["hora_inicio"]:
                racha += 1
            else:
                racha = 1
            maxima = max(maxima, racha)
    return maxima


def secuencia_permitida(restricciones, profesor_id: int, slots_actuales_dia, secuencia) -> bool:
    if any(slot_no_disponible(restricciones, profesor_id, slot) for slot in secuencia):
        return False

    regla = restricciones["reglas"].get(int(profesor_id))
    if not regla:
        return True

    max_dia = regla.get("max_bloques_dia")
    if max_dia is not None and len(slots_actuales_dia) + len(secuencia) > int(max_dia):
        return False

    max_consecutivos = regla.get("max_bloques_consecutivos")
    if max_consecutivos is not None:
        if _racha_maxima(list(slots_actuales_dia) + list(secuencia)) > int(max_consecutivos):
            return False

    return True


def penalizacion_preferencias(restricciones, profesor_id: int, secuencia, orden_minimo: int, orden_maximo: int) -> int:
    regla = restricciones["reglas"].get(int(profesor_id))
    if not regla:
        return 0

    penalizacion = 0
    if regla.get("evitar_primera_hora") and any(int(slot["orden_bloque"]) == orden_minimo for slot in secuencia):
        penalizacion += 220
    if regla.get("evitar_ultima_hora") and any(int(slot["orden_bloque"]) == orden_maximo for slot in secuencia):
        penalizacion += 220
    return penalizacion


def _contar_huecos(slots_dia) -> int:
    ordenados = sorted(slots_dia, key=lambda s: (s["hora_inicio"], s["hora_fin"]))
    huecos = 0
    for indice in range(1, len(ordenados)):
        if ordenados[indice]["hora_inicio"] > ordenados[indice - 1]["hora_fin"]:
            huecos += 1
    return huecos


def contar_preferencias_incumplidas(asignaciones, slots, horario, restricciones) -> int:
    asignaciones_por_id = {int(a["id_asignacion_carga"]): a for a in asignaciones}
    slots_por_clave = {(int(s["id_bloque_tiempo"]), int(s["dia_indice"])): s for s in slots}
    profesor_por_dia = defaultdict(list)
    extremos = {}

    for slot in slots:
        clave = (int(slot["perfil_horario_id"]), int(slot["dia_indice"]))
        orden = int(slot["orden_bloque"])
        if clave not in extremos:
            extremos[clave] = [orden, orden]
        else:
            extremos[clave][0] = min(extremos[clave][0], orden)
            extremos[clave][1] = max(extremos[clave][1], orden)

    for item in horario:
        asig = asignaciones_por_id.get(int(item[2]))
        slot = slots_por_clave.get((int(item[4]), int(item[5])))
        if not asig or not slot:
            continue
        profesor_por_dia[(int(asig["profesor_id"]), int(item[5]))].append(slot)

    incumplimientos = 0
    for (profesor_id, _dia), items in profesor_por_dia.items():
        regla = restricciones["reglas"].get(profesor_id)
        if not regla:
            continue
        if regla.get("evitar_huecos", True):
            incumplimientos += _contar_huecos(items)
        for slot in items:
            minimo, maximo = extremos[(int(slot["perfil_horario_id"]), int(slot["dia_indice"]))]
            orden = int(slot["orden_bloque"])
            if regla.get("evitar_primera_hora") and orden == minimo:
                incumplimientos += 1
            if regla.get("evitar_ultima_hora") and orden == maximo:
                incumplimientos += 1
    return incumplimientos


def validar_horario_restricciones(asignaciones, slots, horario, restricciones):
    asignaciones_por_id = {int(a["id_asignacion_carga"]): a for a in asignaciones}
    slots_por_clave = {(int(s["id_bloque_tiempo"]), int(s["dia_indice"])): s for s in slots}
    profesor_por_dia = defaultdict(list)
    errores = []

    for item in horario:
        asignacion_id = int(item[2])
        bloque_id = int(item[4])
        dia = int(item[5])
        asig = asignaciones_por_id.get(asignacion_id)
        slot = slots_por_clave.get((bloque_id, dia))
        if not asig or not slot:
            continue
        profesor_id = int(asig["profesor_id"])
        if slot_no_disponible(restricciones, profesor_id, slot):
            errores.append(f"{asig['profesor_nombre']} fue asignado en un bloque marcado como no disponible")
        profesor_por_dia[(profesor_id, dia)].append((slot, asig))

    for (profesor_id, _dia), items in profesor_por_dia.items():
        regla = restricciones["reglas"].get(profesor_id)
        if not regla:
            continue
        slots_dia = [slot for slot, _ in items]
        nombre = items[0][1]["profesor_nombre"]
        max_dia = regla.get("max_bloques_dia")
        if max_dia is not None and len(slots_dia) > int(max_dia):
            errores.append(f"{nombre} supera su máximo de {int(max_dia)} bloques por día")
        max_consecutivos = regla.get("max_bloques_consecutivos")
        if max_consecutivos is not None and _racha_maxima(slots_dia) > int(max_consecutivos):
            errores.append(f"{nombre} supera su máximo de {int(max_consecutivos)} bloques consecutivos")

    if errores:
        raise ValueError("El horario incumple restricciones docentes: " + "; ".join(errores[:15]))
    return True
