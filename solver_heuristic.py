from collections import defaultdict
import random

from psycopg2.extras import RealDictCursor

from solver import (
    _cargar_asignaciones,
    _cargar_preferencias_optimizacion,
    _cargar_slots,
    _evaluar_calidad_horario,
    _intervalo_profesor_disponible,
    _particion_consecutiva,
    _rachas_temporales,
    _secuencias_consecutivas,
    _validar_capacidad,
    _validar_periodo,
    _validar_resultado_final,
)


def _hora_a_minutos(valor):
    return int(valor.hour) * 60 + int(valor.minute) + (int(valor.second) / 60 if getattr(valor, "second", 0) else 0)


def _contar_huecos_lista(items, tolerancia_minutos=30):
    ordenados = sorted(items, key=lambda s: (s["hora_inicio"], s["hora_fin"]))
    huecos = 0
    for indice in range(1, len(ordenados)):
        anterior = ordenados[indice - 1]
        actual = ordenados[indice]
        diferencia = _hora_a_minutos(actual["hora_inicio"]) - _hora_a_minutos(anterior["hora_fin"])
        if diferencia > tolerancia_minutos:
            huecos += 1
    return huecos


def _es_adyacente(items, secuencia):
    if not items or not secuencia:
        return False
    for existente in items:
        for nuevo in secuencia:
            if existente["hora_fin"] == nuevo["hora_inicio"] or nuevo["hora_fin"] == existente["hora_inicio"]:
                return True
    return False


def optimizar_horarios_institucion(institucion_id: int, periodo_lectivo_id: int, conn):
    """Genera horarios válidos usando una heurística inicial orientada a calidad y persiste el mejor."""
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        periodo = _validar_periodo(cur, institucion_id, periodo_lectivo_id)
        asignaciones = _cargar_asignaciones(cur, institucion_id, periodo_lectivo_id)
        slots = _cargar_slots(cur, institucion_id, periodo_lectivo_id)
        preferencias = _cargar_preferencias_optimizacion(cur, institucion_id)

        slots_por_perfil = defaultdict(list)
        dias_por_perfil = defaultdict(set)
        for slot in slots:
            perfil_id = int(slot["perfil_horario_id"])
            slots_por_perfil[perfil_id].append(slot)
            dias_por_perfil[perfil_id].add(int(slot["dia_indice"]))

        perfiles_sin_bloques = sorted(
            {
                a["perfil_horario_nombre"]
                for a in asignaciones
                if not slots_por_perfil.get(int(a["perfil_horario_id"]))
            }
        )
        if perfiles_sin_bloques:
            raise ValueError(
                "Los siguientes perfiles no tienen bloques generados para este período: "
                + ", ".join(perfiles_sin_bloques)
                + "."
            )

        _validar_capacidad(asignaciones, slots_por_perfil)

        carga_profesor_total = defaultdict(int)
        carga_grupo_total = defaultdict(int)
        for asig in asignaciones:
            carga_profesor_total[int(asig["profesor_id"])] += int(asig["horas_por_semana"])
            grupo = (int(asig["curso_id"]), int(asig["paralelo_id"]))
            carga_grupo_total[grupo] += int(asig["horas_por_semana"])

        def generar_intento():
            profesor_intervalos = defaultdict(list)
            profesor_slots_dia = defaultdict(list)
            grupo_slots_dia = defaultdict(list)
            grupo_ocupado = set()
            carga_grupo_dia = defaultdict(int)
            horarios = []
            dias_usados = defaultdict(set)
            posiciones_usadas = defaultdict(lambda: defaultdict(int))
            bloques_por_asignacion = defaultdict(list)

            asignaciones_ordenadas = list(asignaciones)
            random.shuffle(asignaciones_ordenadas)
            asignaciones_ordenadas.sort(
                key=lambda a: (
                    -carga_profesor_total[int(a["profesor_id"])],
                    -int(a["horas_por_semana"]),
                    len(slots_por_perfil[int(a["perfil_horario_id"])]),
                )
            )

            for asig in asignaciones_ordenadas:
                asignacion_id = int(asig["id_asignacion_carga"])
                profesor_id = int(asig["profesor_id"])
                grupo = (int(asig["curso_id"]), int(asig["paralelo_id"]))
                perfil_id = int(asig["perfil_horario_id"])
                horas_totales = int(asig["horas_por_semana"])
                permite_consecutivas = bool(asig["permite_consecutivas"])
                max_consecutivas = (
                    max(1, int(asig["max_horas_consecutivas"] or 1))
                    if permite_consecutivas
                    else 1
                )
                particion = _particion_consecutiva(horas_totales, max_consecutivas)

                for tamano_bloque in particion:
                    candidatos = []
                    secuencias = _secuencias_consecutivas(
                        slots_por_perfil[perfil_id], tamano_bloque
                    )

                    for secuencia in secuencias:
                        dia = int(secuencia[0]["dia_indice"])
                        clave_profesor = (profesor_id, dia)
                        clave_grupo = (grupo, dia)

                        if any(
                            not _intervalo_profesor_disponible(
                                profesor_intervalos[clave_profesor],
                                slot["hora_inicio"],
                                slot["hora_fin"],
                            )
                            for slot in secuencia
                        ):
                            continue

                        if any(
                            (grupo, dia, slot["id_bloque_tiempo"]) in grupo_ocupado
                            for slot in secuencia
                        ):
                            continue

                        usados = bloques_por_asignacion[asignacion_id]
                        rachas_resultantes = _rachas_temporales(usados + secuencia)
                        if not permite_consecutivas:
                            if any(racha > 1 for racha in rachas_resultantes):
                                continue
                        elif any(racha > max_consecutivas for racha in rachas_resultantes):
                            continue

                        puntuacion = 0

                        # Distribuir cada materia por distintos días sigue siendo prioritario.
                        puntuacion += 520 if dia not in dias_usados[asignacion_id] else -180

                        # Evitar que una materia caiga siempre en la misma posición del día.
                        for slot in secuencia:
                            orden = int(slot["orden_bloque"])
                            repeticiones = posiciones_usadas[asignacion_id][orden]
                            puntuacion += 110 if repeticiones == 0 else -(repeticiones * 80)

                        # Balancear la carga diaria del curso/paralelo.
                        dias_disponibles = max(1, len(dias_por_perfil[perfil_id]))
                        objetivo_diario = carga_grupo_total[grupo] / dias_disponibles
                        carga_actual = carga_grupo_dia[clave_grupo]
                        carga_resultante = carga_actual + tamano_bloque
                        puntuacion -= int(abs(carga_resultante - objetivo_diario) * 70)
                        puntuacion -= carga_actual * 30

                        # Premiar continuidad y castigar la creación de huecos reales.
                        profesor_actual = profesor_slots_dia[clave_profesor]
                        grupo_actual = grupo_slots_dia[clave_grupo]

                        huecos_profesor_antes = _contar_huecos_lista(profesor_actual)
                        huecos_profesor_despues = _contar_huecos_lista(profesor_actual + secuencia)
                        delta_huecos_profesor = huecos_profesor_despues - huecos_profesor_antes
                        puntuacion -= delta_huecos_profesor * 260

                        huecos_grupo_antes = _contar_huecos_lista(grupo_actual)
                        huecos_grupo_despues = _contar_huecos_lista(grupo_actual + secuencia)
                        delta_huecos_grupo = huecos_grupo_despues - huecos_grupo_antes
                        puntuacion -= delta_huecos_grupo * 180

                        if _es_adyacente(profesor_actual, secuencia):
                            puntuacion += 150
                        if _es_adyacente(grupo_actual, secuencia):
                            puntuacion += 110

                        # Bloques dobles permitidos deben conservar prioridad suficiente.
                        if tamano_bloque > 1:
                            puntuacion += tamano_bloque * 700

                        # Mantiene diversidad entre intentos sin dominar los criterios de calidad.
                        puntuacion += random.randint(0, 60)
                        candidatos.append((puntuacion, secuencia))

                    if not candidatos:
                        return None

                    candidatos.sort(key=lambda item: -item[0])
                    top = min(3, len(candidatos))
                    # 75% el mejor; 25% uno de los siguientes para conservar exploración.
                    if top == 1 or random.random() < 0.75:
                        _, secuencia = candidatos[0]
                    else:
                        _, secuencia = random.choice(candidatos[1:top])

                    dia = int(secuencia[0]["dia_indice"])
                    clave_profesor = (profesor_id, dia)
                    clave_grupo = (grupo, dia)
                    for slot in secuencia:
                        bloque_id = int(slot["id_bloque_tiempo"])
                        orden_bloque = int(slot["orden_bloque"])
                        profesor_intervalos[clave_profesor].append(
                            (slot["hora_inicio"], slot["hora_fin"])
                        )
                        profesor_slots_dia[clave_profesor].append(slot)
                        grupo_slots_dia[clave_grupo].append(slot)
                        grupo_ocupado.add((grupo, dia, bloque_id))
                        carga_grupo_dia[clave_grupo] += 1
                        dias_usados[asignacion_id].add(dia)
                        posiciones_usadas[asignacion_id][orden_bloque] += 1
                        bloques_por_asignacion[asignacion_id].append(slot)
                        horarios.append(
                            (
                                institucion_id,
                                periodo_lectivo_id,
                                asignacion_id,
                                int(asig["aula_id"]),
                                bloque_id,
                                dia,
                            )
                        )

            for asig in asignaciones:
                asignacion_id = int(asig["id_asignacion_carga"])
                horas_totales = int(asig["horas_por_semana"])
                permite = bool(asig["permite_consecutivas"])
                max_consecutivas = (
                    max(1, int(asig["max_horas_consecutivas"] or 1)) if permite else 1
                )
                esperadas = sorted(_particion_consecutiva(horas_totales, max_consecutivas))
                reales = sorted(_rachas_temporales(bloques_por_asignacion[asignacion_id]))
                if reales != esperadas:
                    return None
            return horarios

        resultado_final = None
        calidad_final = None
        intento_utilizado = 0
        intentos_realizados = 0
        candidatos_validos = 0
        max_intentos = 500
        max_candidatos_validos = 20

        for intento in range(1, max_intentos + 1):
            intentos_realizados = intento
            resultado = generar_intento()
            if resultado is None:
                continue

            candidatos_validos += 1
            calidad = _evaluar_calidad_horario(
                asignaciones, slots, resultado, preferencias
            )
            if calidad_final is None or calidad["puntaje"] > calidad_final["puntaje"]:
                resultado_final = resultado
                calidad_final = calidad
                intento_utilizado = intento

            if candidatos_validos >= max_candidatos_validos:
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

        validacion_final = _validar_resultado_final(
            asignaciones, slots, resultado_final
        )
        calidad_final["candidatos_validos_evaluados"] = candidatos_validos
        calidad_final["intentos_realizados"] = intentos_realizados
        calidad_final["heuristica_inicial"] = "continuidad_balance_v2"

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
            "intentos_realizados": intentos_realizados,
            "candidatos_validos_evaluados": candidatos_validos,
            "cursos_paralelos": len(grupos),
            "profesores": len(profesores),
            "perfiles_horarios": len(perfiles),
            "asignaciones": len(asignaciones),
            "validacion": validacion_final,
            "calidad": calidad_final,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
