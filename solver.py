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
        ORDER BY bt.perfil_horario_id, td.dia_indice, bt.orden_bloque, bt.id_bloque_tiempo;
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


def _racha_consecutiva(bloques_asignacion, dia, orden_bloque):
    ordenes = sorted(orden for dia_usado, orden in bloques_asignacion if dia_usado == dia)
    consecutivas = 1
    anterior = orden_bloque - 1
    while anterior in ordenes:
        consecutivas += 1
        anterior -= 1
    return consecutivas


def _intervalo_profesor_disponible(intervalos, inicio, fin):
    return all(fin <= usado_inicio or inicio >= usado_fin for usado_inicio, usado_fin in intervalos)


def optimizar_horarios_institucion(institucion_id: int, periodo_lectivo_id: int, conn):
    """
    Genera horarios por Curso + Paralelo usando el perfil horario asignado.
    Si el curso/paralelo no tiene perfil explícito, usa el perfil general del período + turno.
    Los conflictos docentes se validan por intervalo real de hora, incluso entre perfiles distintos.
    """
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
            asignaciones_ordenadas.sort(key=lambda a: (-int(a["horas_por_semana"]), len(slots_por_perfil[a["perfil_horario_id"]]), a["id_asignacion_carga"]))

            for asig in asignaciones_ordenadas:
                asignacion_id = asig["id_asignacion_carga"]
                profesor_id = asig["profesor_id"]
                grupo = (asig["curso_id"], asig["paralelo_id"])
                horas_totales = int(asig["horas_por_semana"])
                permite_consecutivas = bool(asig["permite_consecutivas"])
                max_consecutivas = max(1, int(asig["max_horas_consecutivas"] or 1)) if permite_consecutivas else 1

                for _ in range(horas_totales):
                    candidatos = []
                    for slot in slots_por_perfil[asig["perfil_horario_id"]]:
                        dia = int(slot["dia_indice"])
                        bloque_id = slot["id_bloque_tiempo"]
                        orden_bloque = int(slot["orden_bloque"])
                        inicio, fin = slot["hora_inicio"], slot["hora_fin"]

                        if not _intervalo_profesor_disponible(profesor_intervalos[(profesor_id, dia)], inicio, fin):
                            continue
                        if (grupo, dia, bloque_id) in grupo_ocupado:
                            continue

                        usados = bloques_por_asignacion[asignacion_id]
                        racha = _racha_consecutiva(usados, dia, orden_bloque)
                        if not permite_consecutivas:
                            if any(d == dia and abs(o - orden_bloque) == 1 for d, o in usados):
                                continue
                        elif racha > max_consecutivas:
                            continue

                        puntuacion = 0
                        puntuacion += 500 if dia not in dias_usados[asignacion_id] else -100
                        rep = posiciones_usadas[asignacion_id][orden_bloque]
                        puntuacion += 300 if rep == 0 else -rep * 180
                        clases_grupo_dia = sum(1 for g, d, _ in grupo_ocupado if g == grupo and d == dia)
                        puntuacion -= clases_grupo_dia * 20
                        if permite_consecutivas and any(d == dia and o == orden_bloque - 1 for d, o in usados):
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
                    profesor_intervalos[(profesor_id, dia)].append((slot["hora_inicio"], slot["hora_fin"]))
                    grupo_ocupado.add((grupo, dia, bloque_id))
                    dias_usados[asignacion_id].add(dia)
                    posiciones_usadas[asignacion_id][orden_bloque] += 1
                    bloques_por_asignacion[asignacion_id].append((dia, orden_bloque))
                    horarios.append((institucion_id, periodo_lectivo_id, asignacion_id, asig["aula_id"], bloque_id, dia))

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
            detalles = [f"{a['curso_nombre']} {a['paralelo_nombre']} · {a['materia_nombre']} · {a['profesor_nombre']} · {int(a['horas_por_semana'])} bloques · perfil {a['perfil_horario_nombre']}" for a in asignaciones]
            raise ValueError("No fue posible generar un horario completo después de " + str(max_intentos) + " intentos. Revisa carga, perfiles, turnos y disponibilidad. Asignaciones: " + "; ".join(detalles))

        horas_generadas = defaultdict(int)
        for horario in resultado_final:
            horas_generadas[horario[2]] += 1
        for asig in asignaciones:
            requeridas = int(asig["horas_por_semana"])
            if horas_generadas[asig["id_asignacion_carga"]] != requeridas:
                raise ValueError("La validación final del horario falló para una asignación.")

        cur.execute("DELETE FROM horarios WHERE institucion_id = %s AND periodo_lectivo_id = %s", (institucion_id, periodo_lectivo_id))
        args_str = b",".join(cur.mogrify("(%s, %s, %s, %s, %s, %s)", item) for item in resultado_final)
        cur.execute(b"INSERT INTO horarios (institucion_id,periodo_lectivo_id,asignacion_carga_id,aula_id,bloque_tiempo_id,dia_indice) VALUES " + args_str)
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
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
