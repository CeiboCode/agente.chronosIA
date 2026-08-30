import argparse
import statistics
import sys
import time

from database import get_db_connection
from solver_quality import optimizar_horarios_institucion

ESCENARIOS = {
    "baseline": {
        "institucion": "Colegio Stress Chronos",
        "periodo": "Stress 2026-2027",
        "seed": "database/seed_motor_stress.sql",
    },
    "hard": {
        "institucion": "Colegio Stress Hard Chronos",
        "periodo": "Stress Hard 2026-2027",
        "seed": "database/seed_motor_stress_hard.sql",
    },
}


def obtener_contexto(conn, escenario):
    config = ESCENARIOS[escenario]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.id_institucion, pl.id_periodo_lectivo
            FROM instituciones i
            JOIN periodos_lectivos pl ON pl.institucion_id = i.id_institucion
            WHERE i.nombre = %s
              AND pl.nombre = %s
            ORDER BY pl.id_periodo_lectivo DESC
            LIMIT 1
            """,
            (config["institucion"], config["periodo"]),
        )
        fila = cur.fetchone()
    if not fila:
        raise RuntimeError(
            f"No existe el escenario '{escenario}'. Ejecuta primero "
            f"{config['seed']} en la base de datos."
        )
    return int(fila[0]), int(fila[1]), config


def validar_bd(conn, institucion_id, periodo_lectivo_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM horarios h
            WHERE h.institucion_id = %s
              AND h.periodo_lectivo_id = %s
            """,
            (institucion_id, periodo_lectivo_id),
        )
        horarios = int(cur.fetchone()[0])

        # Detecta solapes por intervalo real, incluso entre perfiles cuyos
        # bloques tienen IDs y duraciones diferentes.
        cur.execute(
            """
            SELECT COUNT(*)
            FROM horarios h1
            JOIN asignaciones_carga ac1 ON ac1.id_asignacion_carga = h1.asignacion_carga_id
            JOIN bloques_tiempo bt1 ON bt1.id_bloque_tiempo = h1.bloque_tiempo_id
            JOIN horarios h2
              ON h2.institucion_id = h1.institucion_id
             AND h2.periodo_lectivo_id = h1.periodo_lectivo_id
             AND h2.dia_indice = h1.dia_indice
             AND h2.id_horario > h1.id_horario
            JOIN asignaciones_carga ac2 ON ac2.id_asignacion_carga = h2.asignacion_carga_id
            JOIN bloques_tiempo bt2 ON bt2.id_bloque_tiempo = h2.bloque_tiempo_id
            WHERE h1.institucion_id = %s
              AND h1.periodo_lectivo_id = %s
              AND ac1.profesor_id = ac2.profesor_id
              AND bt1.hora_inicio < bt2.hora_fin
              AND bt2.hora_inicio < bt1.hora_fin
            """,
            (institucion_id, periodo_lectivo_id),
        )
        conflictos_docente = int(cur.fetchone()[0])

        cur.execute(
            """
            SELECT COUNT(*)
            FROM horarios h1
            JOIN asignaciones_carga ac1 ON ac1.id_asignacion_carga = h1.asignacion_carga_id
            JOIN aulas a1 ON a1.id_aula = ac1.aula_id
            JOIN bloques_tiempo bt1 ON bt1.id_bloque_tiempo = h1.bloque_tiempo_id
            JOIN horarios h2
              ON h2.institucion_id = h1.institucion_id
             AND h2.periodo_lectivo_id = h1.periodo_lectivo_id
             AND h2.dia_indice = h1.dia_indice
             AND h2.id_horario > h1.id_horario
            JOIN asignaciones_carga ac2 ON ac2.id_asignacion_carga = h2.asignacion_carga_id
            JOIN aulas a2 ON a2.id_aula = ac2.aula_id
            JOIN bloques_tiempo bt2 ON bt2.id_bloque_tiempo = h2.bloque_tiempo_id
            WHERE h1.institucion_id = %s
              AND h1.periodo_lectivo_id = %s
              AND a1.curso_id = a2.curso_id
              AND a1.paralelo_id = a2.paralelo_id
              AND bt1.hora_inicio < bt2.hora_fin
              AND bt2.hora_inicio < bt1.hora_fin
            """,
            (institucion_id, periodo_lectivo_id),
        )
        conflictos_grupo = int(cur.fetchone()[0])

        cur.execute(
            """
            SELECT COUNT(*)
            FROM horarios ho
            JOIN bloques_tiempo bt ON bt.id_bloque_tiempo = ho.bloque_tiempo_id
            WHERE ho.institucion_id = %s
              AND ho.periodo_lectivo_id = %s
              AND bt.es_receso = TRUE
            """,
            (institucion_id, periodo_lectivo_id),
        )
        clases_receso = int(cur.fetchone()[0])

        cur.execute(
            """
            SELECT COUNT(*)
            FROM (
              SELECT ac.id_asignacion_carga,
                     ac.horas_por_semana,
                     COUNT(ho.id_horario) AS creadas
              FROM asignaciones_carga ac
              LEFT JOIN horarios ho
                ON ho.asignacion_carga_id = ac.id_asignacion_carga
               AND ho.institucion_id = ac.institucion_id
               AND ho.periodo_lectivo_id = ac.periodo_lectivo_id
              WHERE ac.institucion_id = %s
                AND ac.periodo_lectivo_id = %s
              GROUP BY ac.id_asignacion_carga, ac.horas_por_semana
              HAVING COUNT(ho.id_horario) <> ac.horas_por_semana
            ) x
            """,
            (institucion_id, periodo_lectivo_id),
        )
        cargas_incompletas = int(cur.fetchone()[0])

        cur.execute(
            """
            SELECT COALESCE(SUM(horas_por_semana), 0)
            FROM asignaciones_carga
            WHERE institucion_id = %s
              AND periodo_lectivo_id = %s
            """,
            (institucion_id, periodo_lectivo_id),
        )
        esperadas = int(cur.fetchone()[0])

    return {
        "horarios": horarios,
        "esperadas": esperadas,
        "conflictos_docente": conflictos_docente,
        "conflictos_grupo": conflictos_grupo,
        "clases_receso": clases_receso,
        "cargas_incompletas": cargas_incompletas,
        "valido": (
            horarios == esperadas
            and conflictos_docente == 0
            and conflictos_grupo == 0
            and clases_receso == 0
            and cargas_incompletas == 0
        ),
    }


def ejecutar(iteraciones, escenario):
    conn = get_db_connection()
    try:
        institucion_id, periodo_lectivo_id, config = obtener_contexto(conn, escenario)
        print(
            f"Escenario: {config['institucion']} | institución={institucion_id} | "
            f"período={periodo_lectivo_id} | iteraciones={iteraciones} | tipo={escenario}"
        )

        tiempos = []
        puntajes = []
        ganancias = []
        fallos = []

        for numero in range(1, iteraciones + 1):
            inicio = time.perf_counter()
            try:
                resultado = optimizar_horarios_institucion(
                    institucion_id,
                    periodo_lectivo_id,
                    conn,
                )
                duracion = time.perf_counter() - inicio
                validacion = validar_bd(conn, institucion_id, periodo_lectivo_id)
                calidad = resultado.get("calidad") or {}
                optimizacion = resultado.get("optimizacion_calidad") or {}
                puntaje = float(calidad.get("puntaje", 0))
                inicial = float(optimizacion.get("puntaje_inicial", puntaje))
                final = float(optimizacion.get("puntaje_final", puntaje))
                ganancia = final - inicial
                intentados = int(optimizacion.get("intercambios_intentados", 0))
                aceptados = int(optimizacion.get("mejoras_aceptadas", 0))

                tiempos.append(duracion)
                puntajes.append(puntaje)
                ganancias.append(ganancia)

                estado = "OK" if validacion["valido"] else "INVALIDO"
                print(
                    f"[{numero:02d}/{iteraciones:02d}] {estado} | "
                    f"{validacion['horarios']}/{validacion['esperadas']} clases | "
                    f"calidad={inicial:.2f}->{final:.2f} (+{ganancia:.2f}) | "
                    f"tiempo={duracion:.2f}s | swaps={aceptados}/{intentados} | "
                    f"choques_doc={validacion['conflictos_docente']} | "
                    f"choques_grupo={validacion['conflictos_grupo']}"
                )

                if not validacion["valido"]:
                    fallos.append((numero, validacion))
            except Exception as error:
                conn.rollback()
                duracion = time.perf_counter() - inicio
                fallos.append((numero, str(error)))
                print(f"[{numero:02d}/{iteraciones:02d}] ERROR | tiempo={duracion:.2f}s | {error}")

        exitos = iteraciones - len(fallos)
        print("\n=== RESUMEN STRESS TEST ===")
        print(f"Escenario: {escenario}")
        print(f"Ejecuciones válidas: {exitos}/{iteraciones}")
        print(f"Tasa de éxito: {(exitos / iteraciones) * 100:.1f}%")
        if tiempos:
            print(f"Tiempo promedio: {statistics.mean(tiempos):.2f}s")
            print(f"Tiempo máximo: {max(tiempos):.2f}s")
        if puntajes:
            print(f"Calidad promedio: {statistics.mean(puntajes):.2f}/100")
            print(f"Calidad mínima: {min(puntajes):.2f}/100")
            print(f"Calidad máxima: {max(puntajes):.2f}/100")
        if ganancias:
            print(f"Ganancia promedio búsqueda local: +{statistics.mean(ganancias):.2f}")
            print(f"Ganancia máxima búsqueda local: +{max(ganancias):.2f}")

        if fallos:
            print("\nFallos detectados:")
            for numero, detalle in fallos:
                print(f"  - ejecución {numero}: {detalle}")
            return 1

        print("\nRESULTADO: motor estable para este escenario.")
        return 0
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Stress test reproducible del motor Chronos IA")
    parser.add_argument(
        "--runs",
        type=int,
        default=20,
        help="Cantidad de generaciones consecutivas (por defecto: 20)",
    )
    parser.add_argument(
        "--scenario",
        choices=sorted(ESCENARIOS.keys()),
        default="baseline",
        help="Escenario a ejecutar: baseline o hard (por defecto: baseline)",
    )
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs debe ser mayor o igual a 1")
    sys.exit(ejecutar(args.runs, args.scenario))


if __name__ == "__main__":
    main()
