import argparse
import statistics
import sys
import time

from database import get_db_connection
from solver_quality import optimizar_horarios_institucion

INSTITUCION_NOMBRE = "Colegio Stress Chronos"
PERIODO_NOMBRE = "Stress 2026-2027"


def obtener_contexto(conn):
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
            (INSTITUCION_NOMBRE, PERIODO_NOMBRE),
        )
        fila = cur.fetchone()
    if not fila:
        raise RuntimeError(
            "No existe el escenario de stress. Ejecuta primero "
            "database/seed_motor_stress.sql en la base de datos."
        )
    return int(fila[0]), int(fila[1])


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

        cur.execute(
            """
            SELECT COUNT(*)
            FROM (
              SELECT h.profesor_id, h.dia_indice, h.bloque_tiempo_id, COUNT(*)
              FROM (
                SELECT ac.profesor_id, ho.dia_indice, ho.bloque_tiempo_id
                FROM horarios ho
                JOIN asignaciones_carga ac ON ac.id_asignacion_carga = ho.asignacion_carga_id
                WHERE ho.institucion_id = %s
                  AND ho.periodo_lectivo_id = %s
              ) h
              GROUP BY h.profesor_id, h.dia_indice, h.bloque_tiempo_id
              HAVING COUNT(*) > 1
            ) x
            """,
            (institucion_id, periodo_lectivo_id),
        )
        conflictos_docente = int(cur.fetchone()[0])

        cur.execute(
            """
            SELECT COUNT(*)
            FROM (
              SELECT a.curso_id, a.paralelo_id, ho.dia_indice, ho.bloque_tiempo_id, COUNT(*)
              FROM horarios ho
              JOIN asignaciones_carga ac ON ac.id_asignacion_carga = ho.asignacion_carga_id
              JOIN aulas a ON a.id_aula = ac.aula_id
              WHERE ho.institucion_id = %s
                AND ho.periodo_lectivo_id = %s
              GROUP BY a.curso_id, a.paralelo_id, ho.dia_indice, ho.bloque_tiempo_id
              HAVING COUNT(*) > 1
            ) x
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


def ejecutar(iteraciones):
    conn = get_db_connection()
    try:
        institucion_id, periodo_lectivo_id = obtener_contexto(conn)
        print(
            f"Escenario: {INSTITUCION_NOMBRE} | institución={institucion_id} | "
            f"período={periodo_lectivo_id} | iteraciones={iteraciones}"
        )

        tiempos = []
        puntajes = []
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
                tiempos.append(duracion)
                puntajes.append(puntaje)

                estado = "OK" if validacion["valido"] else "INVALIDO"
                rondas = optimizacion.get("rondas_ejecutadas", 1)
                evaluados = optimizacion.get("puntajes_evaluados", [puntaje])
                print(
                    f"[{numero:02d}/{iteraciones:02d}] {estado} | "
                    f"{validacion['horarios']}/{validacion['esperadas']} clases | "
                    f"calidad={puntaje:.2f} | tiempo={duracion:.2f}s | "
                    f"rondas={rondas} | evaluados={evaluados}"
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
        print(f"Ejecuciones válidas: {exitos}/{iteraciones}")
        print(f"Tasa de éxito: {(exitos / iteraciones) * 100:.1f}%")
        if tiempos:
            print(f"Tiempo promedio: {statistics.mean(tiempos):.2f}s")
            print(f"Tiempo máximo: {max(tiempos):.2f}s")
        if puntajes:
            print(f"Calidad promedio: {statistics.mean(puntajes):.2f}/100")
            print(f"Calidad mínima: {min(puntajes):.2f}/100")
            print(f"Calidad máxima: {max(puntajes):.2f}/100")

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
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs debe ser mayor o igual a 1")
    sys.exit(ejecutar(args.runs))


if __name__ == "__main__":
    main()
