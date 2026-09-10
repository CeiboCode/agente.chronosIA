def cargar_restricciones_docentes(cur, institucion_id: int, periodo_lectivo_id: int):
    """Carga únicamente los bloques en los que cada docente no puede dar clases.

    Se conserva la clave ``reglas`` vacía por compatibilidad con el resto del
    motor mientras terminamos de retirar la antigua configuración de límites y
    preferencias docentes.
    """
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
    return {"reglas": {}, "no_disponibles": no_disponibles}


def slot_no_disponible(restricciones, profesor_id: int, slot) -> bool:
    """Indica si un slot está prohibido explícitamente para el docente."""
    return (
        int(profesor_id),
        int(slot["perfil_horario_id"]),
        int(slot["dia_indice"]),
        slot["hora_inicio"],
        slot["hora_fin"],
    ) in restricciones["no_disponibles"]


def secuencia_permitida(restricciones, profesor_id: int, slots_actuales_dia, secuencia) -> bool:
    """Una secuencia es válida si ninguno de sus bloques está marcado como no disponible."""
    return not any(
        slot_no_disponible(restricciones, profesor_id, slot)
        for slot in secuencia
    )


def penalizacion_preferencias(restricciones, profesor_id: int, secuencia, orden_minimo: int, orden_maximo: int) -> int:
    """Compatibilidad temporal: ya no existen preferencias docentes configurables."""
    return 0


def contar_preferencias_incumplidas(asignaciones, slots, horario, restricciones) -> int:
    """Compatibilidad temporal: ya no se contabilizan preferencias docentes."""
    return 0


def validar_horario_restricciones(asignaciones, slots, horario, restricciones):
    """Valida que ningún docente sea asignado en una hora marcada como no disponible."""
    asignaciones_por_id = {
        int(a["id_asignacion_carga"]): a
        for a in asignaciones
    }
    slots_por_clave = {
        (int(s["id_bloque_tiempo"]), int(s["dia_indice"])): s
        for s in slots
    }
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
            errores.append(
                f"{asig['profesor_nombre']} fue asignado en una hora marcada como no disponible"
            )

    if errores:
        raise ValueError(
            "El horario incumple la disponibilidad docente: "
            + "; ".join(errores[:15])
        )

    return True
