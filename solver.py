from collections import defaultdict
from psycopg2.extras import RealDictCursor
import random


def _validar_periodo(cur, institucion_id, periodo_lectivo_id):
    cur.execute(
        """
        SELECT id_periodo_lectivo, nombre, fecha_inicio, fecha_fin, activo
        FROM periodos_lectivos
        WHERE id_periodo_lectivo = %s AND institucion_id = %s
        LIMIT 1;
        """,
        (periodo_lectivo_id, institucion_id),
    )
    periodo = cur.fetchone()
    if not periodo:
        raise ValueError("El período lectivo no existe o no pertenece a la institución.")
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
            t.nombre AS turno_nombre,
            COALESCE(cpt.perfil_horario_id, ph_default.id_perfil_horario) AS perfil_horario_id,
            COALESCE(ph_asignado.nombre, ph_default.nombre) AS perfil_horario_nombre
        FROM asignaciones_carga ac
        INNER JOIN profesores p ON p.id_profesor = ac.profesor_id AND p.institucion_id = ac.institucion_id
        INNER JOIN aulas a ON a.id_aula = ac.aula_id AND a.institucion_id = ac.institucion_id
        INNER JOIN cursos c ON c.id_curso = a.curso_id AND c.institucion_id = ac.institucion_id
        INNER JOIN paralelos pa ON pa.id_paralelo = a.paralelo_id AND pa.curso_id = a.curso_id
        INNER JOIN materias m ON m.id_materia = a.materia_id AND m.institucion_id = ac.institucion_id
        LEFT JOIN curso_paralelo_turnos cpt
            ON cpt.institucion_id = ac.institucion_id
           AND cpt.periodo_lectivo_id = ac.periodo_lectivo_id
           AND cpt.curso_id = a.curso_id
           AND cpt.paralelo_id = a.paralelo_id
        LEFT JOIN turnos t ON t.id_turno = cpt.turno_id AND t.institucion_id = ac.institucion_id
        LEFT JOIN perfiles_horarios ph_asignado ON ph_asignado.id_perfil_horario = cpt.perfil_horario_id
        LEFT JOIN perfiles_horarios ph_default
            ON ph_default.institucion_id = ac.institucion_id
           AND ph_default.periodo_lectivo_id = ac.periodo_lectivo_id
           AND ph_default.turno_id = cpt.turno_id
           AND ph_default.es_predeterminado = TRUE
        WHERE ac.institucion_id = %s
          AND ac.periodo_lectivo_id = %s
          AND ac.horas_por_semana > 0
        ORDER BY ac.horas_por_semana DESC, ac.id_asignacion_carga;
        """,
        (institucion_id, periodo_lectivo_id),
    )
    asignaciones = cur.fetchall()
    if not asignaciones:
        raise ValueError("No existen asignaciones de carga horaria registradas para este período.")

    sin_turno = [a for a in asignaciones if not a["turno_id"]]
    if sin_turno:
        faltantes = sorted({f"{a['curso_nombre']} {a['paralelo_nombre']}" for a in sin_turno})
        raise ValueError("Hay cursos/paralelos sin turno asignado para este período: " + ", ".join(faltantes) + ".")

    sin_perfil = [a for a in asignaciones if not a["perfil_horario_id"]]
    if sin_perfil:
        faltantes = sorted({f"{a['curso_nombre']} {a['paralelo_nombre']}" for a in sin_perfil})
        raise ValueError("Hay cursos/paralelos sin perfil horario asignado y sin perfil general disponible: " + ", ".join(faltantes) + ".")

    return asignaciones


def _cargar_slots(cur, institucion_id, periodo_lectivo_id):
    cur.execute(
        """
        SELECT
            bt.id_bloque_tiempo,
            bt.turno_id,
            bt.periodo_lectivo_id,
            bt.perfil_horario_id,
            bt.orden_bloque,
            bt.hora_inicio,
            bt.hora_fin,
            bt.es_receso,
            ph.nombre AS perfil_horario_nombre,
            t.nombre AS turno_nombre,
            td.dia_indice,
            td.es_dia_laboral
        FROM bloques_tiempo bt
        INNER JOIN turnos t ON t.id_turno = bt.turno_id
        INNER JOIN turnos_dias td ON td.turno_id = t.id_turno
        LEFT JOIN perfiles_horarios ph ON ph.id_perfil_horario = bt.perfil_horario_id
        WHERE t.institucion_id = %s
          AND bt.periodo_lectivo_id = %s
          AND bt.perfil_horario_id IS NOT NULL
          AND bt.es_receso = FALSE
          AND td.es_dia_laboral = TRUE
        ORDER BY bt.perfil_horario_id, td.dia_indice, bt.hora_inicio, bt.id_bloque_tiempo;
        """,
        (institucion_id, periodo_lectivo_id),
    )
    slots = cur.fetchall()
    if not slots:
        raise ValueError("No existen bloques de tiempo laborables para los perfiles del período seleccionado.")
    return slots


def _validar_capacidad(asignaciones, slots_por_perfil):
    demanda_por_grupo = defaultdict(int)
    grupos_meta = {}
    for asig in asignaciones:
        grupo = (asig["curso_id"], asig["paralelo_id"])
        demanda_por_grupo[grupo] += int(asig["horas_por_semana"])
        grupos_meta[grupo] = asig

    problemas = []
    for grupo, demanda in demanda_por_grupo.items():
        meta = grupos_meta[grupo]
        capacidad = len(slots_por_perfil.get(meta["perfil_horario_id"], []))
        if demanda > capacidad:
            problemas.append(
                f"{meta['curso_nombre']} {meta['paralelo_nombre']}: requiere {demanda} bloques y el perfil {meta['perfil_horario_nombre']} ofrece {capacidad}"
            )
    if problemas:
        raise ValueError("La carga semanal excede la capacidad disponible de algunos cursos/paralelos. " + "; ".join(problemas))


def _intervalo_profesor_disponible(intervalos, inicio, fin):
    return all(fin <= usado_inicio or inicio >= usado_fin for usado_inicio, usado_fin in intervalos)


def _slots_son_consecutivos(anterior, siguiente):
    """Consecutivo real: misma jornada y sin hueco entre fin e inicio."""
    return (
        int(anterior["dia_indice"]) == int(siguiente["dia_indice"])
        and anterior["hora_fin"] == siguiente["hora_inicio"]
    )


def _rachas_temporales(bloques):
    """Devuelve tamaños de rachas usando horas reales; un recreo/hueco corta la racha."""
    por_dia = defaultdict(list)
    for bloque in bloques:
        por_dia[int(bloque["dia_indice"])].append(bloque)

    rachas = []
    for _, items in por_dia.items():
        items = sorted(items, key=lambda b: (b["hora_inicio"], b["hora_fin"]))
        if not items:
            continue
        longitud = 1
        for i in range(1, len(items)):
            if _slots_son_consecutivos(items[i - 1], items[i]):
                longitud += 1
            else:
                rachas.append(longitud)
                longitud = 1
        rachas.append(longitud)
    return rachas


def _particion_consecutiva(horas_totales, max_consecutivas):
    """Ej.: 5 horas con máximo 2 => [2, 2, 1]."""
    if max_consecutivas <= 1:
        return [1] * horas_totales
    partes = []
    restantes = horas_totales
    while restantes > 0:
        tamano = min(max_consecutivas, restantes)
        partes.append(tamano)
        restantes -= tamano
    return partes


def _secuencias_consecutivas(slots, tamano):
    """Construye secuencias del mismo día con adyacencia temporal exacta."""
    if tamano <= 1:
        return [[slot] for slot in slots]

    por_dia = defaultdict(list)
    for slot in slots:
        por_dia[int(slot["dia_indice"])].append(slot)

    secuencias = []
    for _, items in por_dia.items():
        items = sorted(items, key=lambda s: (s["hora_inicio"], s["hora_fin"]))
        for i in range(0, len(items) - tamano + 1):
            secuencia = items[i : i + tamano]
            if all(_slots_son_consecutivos(secuencia[j - 1], secuencia[j]) for j in range(1, tamano)):
                secuencias.append(secuencia)
    return secuencias


def _validar_resultado_final(asignaciones, slots, resultado):
    """Audita el horario completo antes de persistirlo."""
    asignaciones_por_id = {int(a["id_asignacion_carga"]): a for a in asignaciones}
    slots_por_clave = {
        (int(s["id_bloque_tiempo"]), int(s["dia_indice"])): s
        for s in slots
    }

    horas_por_asignacion = defaultdict(int)
    bloques_por_asignacion = defaultdict(list)
    profesor_por_dia = defaultdict(list)
    grupo_por_dia = defaultdict(list)
    errores = []

    for item in resultado:
        _, _, asignacion_id, aula_id, bloque_id, dia = item
        asignacion_id = int(asignacion_id)
        bloque_id = int(bloque_id)
        dia = int(dia)
        asig = asignaciones_por_id.get(asignacion_id)
        slot = slots_por_clave.get((bloque_id, dia))

        if asig is None:
            errores.append(f"Asignación {asignacion_id} no existe en la carga del período.")
            continue
        if slot is None:
            errores.append(f"Asignación {asignacion_id} usa un bloque inexistente, no laborable o de receso: {bloque_id} día {dia}.")
            continue
        if int(aula_id) != int(asig["aula_id"]):
            errores.append(f"Asignación {asignacion_id} fue guardada con un aula académica incorrecta.")
            continue
        if int(slot["perfil_horario_id"]) != int(asig["perfil_horario_id"]):
            errores.append(
                f"{asig['curso_nombre']} {asig['paralelo_nombre']} · {asig['materia_nombre']} usa un bloque de un perfil distinto al asignado."
            )
            continue
        if bool(slot.get("es_receso")):
            errores.append(
                f"{asig['curso_nombre']} {asig['paralelo_nombre']} · {asig['materia_nombre']} fue colocado sobre un recreo."
            )
            continue

        horas_por_asignacion[asignacion_id] += 1
        bloques_por_asignacion[asignacion_id].append(slot)
        profesor_por_dia[(int(asig["profesor_id"]), dia)].append((slot, asig))
        grupo_por_dia[((int(asig["curso_id"]), int(asig["paralelo_id"])), dia)].append((slot, asig))

    for asig in asignaciones:
        asignacion_id = int(asig["id_asignacion_carga"])
        requeridas = int(asig["horas_por_semana"])
        generadas = horas_por_asignacion[asignacion_id]
        if generadas != requeridas:
            errores.append(
                f"{asig['curso_nombre']} {asig['paralelo_nombre']} · {asig['materia_nombre']}: requiere {requeridas} bloques y se generaron {generadas}."
            )

        permite = bool(asig["permite_consecutivas"])
        max_consecutivas = max(1, int(asig["max_horas_consecutivas"] or 1)) if permite else 1
        esperadas = sorted(_particion_consecutiva(requeridas, max_consecutivas))
        reales = sorted(_rachas_temporales(bloques_por_asignacion[asignacion_id]))
        if reales != esperadas:
            errores.append(
                f"{asig['curso_nombre']} {asig['paralelo_nombre']} · {asig['materia_nombre']}: distribución consecutiva inválida; esperada {esperadas}, obtenida {reales}."
            )

    def validar_solapamientos(contenedor, etiqueta):
        conflictos = 0
        for _, items in contenedor.items():
            ordenados = sorted(items, key=lambda x: (x[0]["hora_inicio"], x[0]["hora_fin"]))
            for indice in range(1, len(ordenados)):
                anterior_slot, anterior_asig = ordenados[indice - 1]
                actual_slot, actual_asig = ordenados[indice]
                if actual_slot["hora_inicio"] < anterior_slot["hora_fin"]:
                    conflictos += 1
                    errores.append(
                        f"Choque de {etiqueta}: "
                        f"{anterior_asig['curso_nombre']} {anterior_asig['paralelo_nombre']} · {anterior_asig['materia_nombre']} "
                        f"({anterior_slot['hora_inicio']}-{anterior_slot['hora_fin']}) con "
                        f"{actual_asig['curso_nombre']} {actual_asig['paralelo_nombre']} · {actual_asig['materia_nombre']} "
                        f"({actual_slot['hora_inicio']}-{actual_slot['hora_fin']})."
                    )
        return conflictos

    conflictos_profesor = validar_solapamientos(profesor_por_dia, "docente")
    conflictos_grupo = validar_solapamientos(grupo_por_dia, "Curso + Paralelo")

    if errores:
        detalle = "; ".join(errores[:20])
        if len(errores) > 20:
            detalle += f"; y {len(errores) - 20} errores adicionales."
        raise ValueError("La validación final del horario detectó inconsistencias: " + detalle)

    return {
        "valido": True,
        "conflictos_docentes": conflictos_profesor,
        "conflictos_cursos_paralelos": conflictos_grupo,
        "cargas_completas": len(asignaciones),
        "consecutivas_ok": len(asignaciones),
        "clases_en_receso": 0,
        "horarios_validados": len(resultado),
    }


def optimizar_horarios_institucion(institucion_id: int, periodo_lectivo_id: int, conn):
    """Genera horarios por Curso + Paralelo respetando perfiles y consecutivas reales por materia."""
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        periodo = _validar_periodo(cur, institucion_id, periodo_lectivo_id)
        asignaciones = _cargar_asignaciones(cur, institucion_id, periodo_lectivo_id)
        slots = _cargar_slots(cur, institucion_id, periodo_lectivo_id)

        slots_por_perfil = defaultdict(list)
        for slot in slots:
            slots_por_perfil[slot["perfil_horario_id"]].append(slot)

        perfiles_sin_bloques = sorted({a["perfil_horario_nombre"] for a in asignaciones if not slots_por_perfil.get(a["perfil_horario_id"])})
        if perfiles_sin_bloques:
            raise ValueError("Los siguientes perfiles no tienen bloques generados para este período: " + ", ".join(perfiles_sin_bloques) + ".")

        _validar_capacidad(asignaciones, slots_por_perfil)

        def generar_intento():
            profesor_intervalos = defaultdict(list)
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
                    len(slots_por_perfil[a["perfil_horario_id"]]),
                    a["id_asignacion_carga"],
                )
            )

            for asig in asignaciones_ordenadas:
                asignacion_id = asig["id_asignacion_carga"]
                profesor_id = asig["profesor_id"]
                grupo = (asig["curso_id"], asig["paralelo_id"])
                horas_totales = int(asig["horas_por_semana"])
                permite_consecutivas = bool(asig["permite_consecutivas"])
                max_consecutivas = max(1, int(asig["max_horas_consecutivas"] or 1)) if permite_consecutivas else 1
                particion = _particion_consecutiva(horas_totales, max_consecutivas)

                for tamano_bloque in particion:
                    candidatos = []
                    secuencias = _secuencias_consecutivas(
                        slots_por_perfil[asig["perfil_horario_id"]],
                        tamano_bloque,
                    )

                    for secuencia in secuencias:
                        dia = int(secuencia[0]["dia_indice"])

                        if any(
                            not _intervalo_profesor_disponible(
                                profesor_intervalos[(profesor_id, dia)],
                                slot["hora_inicio"],
                                slot["hora_fin"],
                            )
                            for slot in secuencia
                        ):
                            continue

                        if any((grupo, dia, slot["id_bloque_tiempo"]) in grupo_ocupado for slot in secuencia):
                            continue

                        usados = bloques_por_asignacion[asignacion_id]
                        rachas_resultantes = _rachas_temporales(usados + secuencia)

                        if not permite_consecutivas:
                            if any(racha > 1 for racha in rachas_resultantes):
                                continue
                        elif any(racha > max_consecutivas for racha in rachas_resultantes):
                            continue

                        puntuacion = 0
                        puntuacion += 500 if dia not in dias_usados[asignacion_id] else -100

                        for slot in secuencia:
                            orden = int(slot["orden_bloque"])
                            rep = posiciones_usadas[asignacion_id][orden]
                            puntuacion += 120 if rep == 0 else -rep * 90

                        clases_grupo_dia = sum(1 for g, d, _ in grupo_ocupado if g == grupo and d == dia)
                        puntuacion -= clases_grupo_dia * 20

                        if tamano_bloque > 1:
                            puntuacion += tamano_bloque * 700

                        puntuacion += random.randint(0, 100)
                        candidatos.append((puntuacion, secuencia))

                    if not candidatos:
                        return None

                    candidatos.sort(key=lambda item: -item[0])
                    _, secuencia = random.choice(candidatos[: min(3, len(candidatos))])
                    dia = int(secuencia[0]["dia_indice"])

                    for slot in secuencia:
                        bloque_id = slot["id_bloque_tiempo"]
                        orden_bloque = int(slot["orden_bloque"])
                        profesor_intervalos[(profesor_id, dia)].append((slot["hora_inicio"], slot["hora_fin"]))
                        grupo_ocupado.add((grupo, dia, bloque_id))
                        dias_usados[asignacion_id].add(dia)
                        posiciones_usadas[asignacion_id][orden_bloque] += 1
                        bloques_por_asignacion[asignacion_id].append(slot)
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

            for asig in asignaciones:
                asignacion_id = asig["id_asignacion_carga"]
                horas_totales = int(asig["horas_por_semana"])
                permite = bool(asig["permite_consecutivas"])
                max_consecutivas = max(1, int(asig["max_horas_consecutivas"] or 1)) if permite else 1
                esperadas = sorted(_particion_consecutiva(horas_totales, max_consecutivas))
                reales = sorted(_rachas_temporales(bloques_por_asignacion[asignacion_id]))
                if reales != esperadas:
                    return None

            return horarios

        resultado_final = None
        intento_utilizado = 0
        max_intentos = 500
        for intento in range(1, max_intentos + 1):
            resultado = generar_intento()
            if resultado is not None:
                resultado_final = resultado
                intento_utilizado = intento
                break

        if resultado_final is None:
            detalles = [
                f"{a['curso_nombre']} {a['paralelo_nombre']} · {a['materia_nombre']} · {a['profesor_nombre']} · {int(a['horas_por_semana'])} bloques · perfil {a['perfil_horario_nombre']}"
                for a in asignaciones
            ]
            raise ValueError(
                "No fue posible generar un horario completo después de "
                + str(max_intentos)
                + " intentos. Revisa carga, perfiles, turnos, consecutivas y disponibilidad. Asignaciones: "
                + "; ".join(detalles)
            )

        validacion_final = _validar_resultado_final(asignaciones, slots, resultado_final)

        cur.execute(
            "DELETE FROM horarios WHERE institucion_id = %s AND periodo_lectivo_id = %s",
            (institucion_id, periodo_lectivo_id),
        )
        args_str = b",".join(
            cur.mogrify("(%s, %s, %s, %s, %s, %s)", item)
            for item in resultado_final
        )
        cur.execute(
            b"INSERT INTO horarios (institucion_id,periodo_lectivo_id,asignacion_carga_id,aula_id,bloque_tiempo_id,dia_indice) VALUES "
            + args_str
        )
        conn.commit()

        grupos = {(a["curso_id"], a["paralelo_id"]) for a in asignaciones}
        profesores = {a["profesor_id"] for a in asignaciones}
        perfiles = {a["perfil_horario_id"] for a in asignaciones}
        return {
            "success": True,
            "institucion_id": institucion_id,
            "periodo_lectivo_id": periodo_lectivo_id,
            "periodo_nombre": periodo["nombre"],
            "horarios_creados": len(resultado_final),
            "intentos_utilizados": intento_utilizado,
            "cursos_paralelos": len(grupos),
            "profesores": len(profesores),
            "perfiles_horarios": len(perfiles),
            "asignaciones": len(asignaciones),
            "validacion": validacion_final,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
