import psycopg2
from psycopg2.extras import RealDictCursor

def optimizar_horarios_institucion(institucion_id: int, periodo_lectivo_id: int, conn):
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # 1. Limpiar horarios anteriores
        cur.execute(
            "DELETE FROM horarios WHERE institucion_id = %s AND periodo_lectivo_id = %s;",
            (institucion_id, periodo_lectivo_id)
        )

        # 2. Obtener asignaciones de carga horaria con reglas de materias
        cur.execute("""
            SELECT ac.id_asignacion_carga, ac.profesor_id, ac.materia_id, 
                   ac.nombre_paralelo, ac.horas_por_semana, m.permite_consecutivas, m.max_horas_consecutivas
            FROM asignaciones_carga ac
            JOIN materias m ON ac.materia_id = m.id_materia
            WHERE ac.institucion_id = %s AND ac.periodo_lectivo_id = %s
            ORDER BY ac.horas_por_semana DESC; -- Asignaturas con más carga van primero
        """, (institucion_id, periodo_lectivo_id))
        asignaciones = cur.fetchall()

        if not asignaciones:
            raise Exception("No existen asignaciones de carga horaria registradas para este periodo.")

        # 3. Obtener aulas disponibles
        cur.execute("SELECT id_aula FROM aulas WHERE institucion_id = %s ORDER BY id_aula;", (institucion_id,))
        aulas_resultado = cur.fetchall()
        if not aulas_resultado:
            raise Exception("Se requiere al menos un aula registrada.")
        
        aulas_disponibles = [a["id_aula"] for a in aulas_resultado]

        # 4. Obtener bloques de tiempo ordenados por día y orden de bloque
        cur.execute("""
            SELECT bt.id_bloque_tiempo, bt.turno_id, td.dia_indice, bt.orden_bloque
            FROM bloques_tiempo bt
            JOIN turnos t ON bt.turno_id = t.id_turno
            JOIN turnos_dias td ON t.id_turno = td.turno_id
            WHERE t.institucion_id = %s AND bt.es_receso = FALSE AND td.es_dia_laboral = TRUE
            ORDER BY td.dia_indice, bt.orden_bloque;
        """, (institucion_id,))
        slots_disponibles = cur.fetchall()

        if not slots_disponibles:
            raise Exception("No hay bloques de tiempo o días laborables configurados.")

        # Agrupar slots por día para facilitar la distribución por curso
        dias_slots = {}
        for slot in slots_disponibles:
            dia = slot["dia_indice"]
            if dia not in dias_slots:
                dias_slots[dia] = []
            dias_slots[dia].append(slot)

        # Estructuras de control globales de ocupación
        profes_ocupados = set()  # (profesor_id, dia, bloque_id)
        paralelos_ocupados = set() # (nombre_paralelo, dia, bloque_id)
        aulas_ocupadas = set()     # (aula_id, dia, bloque_id)

        horarios_a_insertar = []

        # 5. Distribución estructurada por Curso / Paralelo
        # Procesamos cada asignación intentando agrupar sus horas de manera lógica
        for asig in asignaciones:
            asignacion_id = asig["id_asignacion_carga"]
            profesor_id = asig["profesor_id"]
            paralelo = asig["nombre_paralelo"]
            horas_totales = asig["horas_por_semana"]
            permite_consecutivas = asig["permite_consecutivas"]
            max_consecutivas = asig["max_horas_consecutivas"] if permite_consecutivas else 1

            horas_asignadas_materia = 0
            
            # Intentar distribuir las horas de esta materia a lo largo de los días disponibles
            # Para evitar que caigan dos clases el mismo día, preferimos un día por hora/bloque
            dias_disponibles_orden = list(dias_slots.keys())
            
            # Bucle para colocar todas las horas requeridas de esta materia
            intentos_distribucion = 0
            while horas_asignadas_materia < horas_totales and intentos_distribucion < 50:
                intentos_distribucion += 1
                
                for dia in dias_disponibles_orden:
                    if horas_asignadas_materia >= horas_totales:
                        break

                    bloques_del_dia = dias_slots[dia]
                    
                    # Determinar cuántas horas seguidas podemos poner en este día para este paralelo
                    # Buscamos un bloque libre para iniciar
                    for i, slot in enumerate(bloques_del_dia):
                        bloque_id = slot["id_bloque_tiempo"]
                        
                        key_prof = (profesor_id, dia, bloque_id)
                        key_paral = (paralelo, dia, bloque_id)

                        # Validar si el profe o el paralelo están libres en este bloque
                        if key_prof in profes_ocupados or key_paral in paralelos_ocupados:
                            continue

                        # Buscar aula disponible
                        aula_seleccionada = None
                        for aula_id in aulas_disponibles:
                            if (aula_id, dia, bloque_id) not in aulas_ocupadas:
                                aula_seleccionada = aula_id
                                break
                        
                        if aula_seleccionada is None:
                            continue

                        # Si permite consecutivas y necesitamos más horas, podemos intentar tomar bloques consecutivos
                        bloques_a_usar = [slot]
                        aulas_a_usar = [aula_seleccionada]
                        
                        horas_a_tomar = min(max_consecutivas, horas_totales - horas_asignadas_materia)
                        
                        # Verificar si los siguientes bloques están libres para el mismo paralelo y profesor
                        valido_consecutivo = True
                        for j in range(1, horas_a_tomar):
                            if i + j >= len(bloques_del_dia):
                                valido_consecutivo = False
                                break
                            
                            next_slot = bloques_del_dia[i + j]
                            next_bloque_id = next_slot["id_bloque_tiempo"]
                            
                            next_key_prof = (profesor_id, dia, next_bloque_id)
                            next_key_paral = (paralelo, dia, next_bloque_id)
                            
                            if next_key_prof in profes_ocupados or next_key_paral in paralelos_ocupados:
                                valido_consecutivo = False
                                break
                            
                            # Buscar aula para el siguiente bloque
                            next_aula = None
                            for aula_id in aulas_disponibles:
                                if (aula_id, dia, next_bloque_id) not in aulas_ocupadas:
                                    next_aula = aula_id
                                    break
                            
                            if next_aula is None:
                                valido_consecutivo = False
                                break
                            
                            bloques_a_usar.append(next_slot)
                            aulas_a_usar.append(next_aula)

                        # Si encontramos los bloques válidos, los registramos formalmente
                        for b_idx, s_item in enumerate(bloques_a_usar):
                            b_id = s_item["id_bloque_tiempo"]
                            a_id = aulas_a_usar[b_idx]
                            
                            profes_ocupados.add((profesor_id, dia, b_id))
                            paralelos_ocupados.add((paralelo, dia, b_id))
                            aulas_ocupadas.add((a_id, dia, b_id))

                            horarios_a_insertar.append((
                                institucion_id,
                                periodo_lectivo_id,
                                asignacion_id,
                                a_id,
                                b_id,
                                dia
                            ))
                            horas_asignadas_materia += 1

                        break # Salir del bucle de bloques de este día y pasar al siguiente

        if len(horarios_a_insertar) == 0:
            raise Exception("No se pudo generar ningún horario estructurado. Verifique la disponibilidad de aulas y bloques.")

        # 6. Inserción masiva optimizada en la base de datos
        if horarios_a_insertar:
            args_str = b",".join(
                cur.mogrify("(%s, %s, %s, %s, %s, %s)", item) for item in horarios_a_insertar
            )
            cur.execute(b"""
                INSERT INTO horarios (institucion_id, periodo_lectivo_id, asignacion_carga_id, aula_id, bloque_tiempo_id, dia_indice)
                VALUES 
            """ + args_str)

        conn.commit()
        cur.close()
        return {"success": True, "horarios_creados": len(horarios_a_insertar)}

    except Exception as e:
        conn.rollback()
        cur.close()
        raise e