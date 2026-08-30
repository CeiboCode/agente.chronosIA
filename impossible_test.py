import argparse
import sys
import time

from database import get_db_connection
from solver_quality import optimizar_horarios_institucion

CASOS = {
    "capacity": {
        "institucion": "Colegio Stress Impossible Capacity",
        "periodo": "Impossible Capacity 2026-2027",
        "espera_fragmento": "excede la capacidad disponible",
        "descripcion": "grupo con 36 bloques requeridos y solo 35 disponibles",
    },
    "teacher": {
        "institucion": "Colegio Stress Impossible Teacher",
        "periodo": "Impossible Teacher 2026-2027",
        "espera_fragmento": "capacidad disponible de algunos docentes",
        "descripcion": "un docente compartido requiere 40 bloques y solo dispone de 35",
    },
}


def obtener_contexto(conn, caso):
    config = CASOS[caso]
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
            "No existe el escenario imposible. Ejecuta primero "
            "database/seed_motor_stress_impossible.sql en la base de datos."
        )
    return int(fila[0]), int(fila[1]), config


def contar_horarios(conn, institucion_id, periodo_lectivo_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM horarios
            WHERE institucion_id = %s
              AND periodo_lectivo_id = %s
            """,
            (institucion_id, periodo_lectivo_id),
        )
        return int(cur.fetchone()[0])


def ejecutar_caso(conn, caso):
    institucion_id, periodo_lectivo_id, config = obtener_contexto(conn, caso)
    print(
        f"Caso: {caso} | {config['descripcion']} | "
        f"institución={institucion_id} | período={periodo_lectivo_id}"
    )

    inicio = time.perf_counter()
    try:
        resultado = optimizar_horarios_institucion(
            institucion_id,
            periodo_lectivo_id,
            conn,
        )
        duracion = time.perf_counter() - inicio
        horarios = contar_horarios(conn, institucion_id, periodo_lectivo_id)
        print(
            f"FALLO | el motor generó una solución cuando debía rechazarla | "
            f"tiempo={duracion:.2f}s | horarios={horarios} | resultado={resultado}"
        )
        return False
    except ValueError as error:
        conn.rollback()
        duracion = time.perf_counter() - inicio
        mensaje = str(error)
        horarios = contar_horarios(conn, institucion_id, periodo_lectivo_id)
        mensaje_ok = config["espera_fragmento"].lower() in mensaje.lower()
        limpio = horarios == 0
        estado = "OK" if mensaje_ok and limpio else "FALLO"
        print(
            f"{estado} | rechazo controlado | tiempo={duracion:.2f}s | "
            f"horarios_persistidos={horarios}"
        )
        print(f"Mensaje: {mensaje}")
        if not mensaje_ok:
            print(
                "Motivo: el mensaje no contiene el diagnóstico esperado: "
                f"'{config['espera_fragmento']}'."
            )
        if not limpio:
            print("Motivo: quedaron horarios parciales persistidos tras el fallo.")
        return mensaje_ok and limpio
    except Exception as error:
        conn.rollback()
        duracion = time.perf_counter() - inicio
        horarios = contar_horarios(conn, institucion_id, periodo_lectivo_id)
        print(
            f"FALLO | excepción inesperada {type(error).__name__} | "
            f"tiempo={duracion:.2f}s | horarios_persistidos={horarios} | {error}"
        )
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Valida que Chronos IA rechace escenarios matemáticamente imposibles"
    )
    parser.add_argument(
        "--case",
        choices=["all", *CASOS.keys()],
        default="all",
        help="Caso imposible a ejecutar (por defecto: all)",
    )
    args = parser.parse_args()

    casos = list(CASOS.keys()) if args.case == "all" else [args.case]
    conn = get_db_connection()
    try:
        resultados = []
        for caso in casos:
            print("\n" + "=" * 72)
            resultados.append((caso, ejecutar_caso(conn, caso)))

        print("\n=== RESUMEN IMPOSSIBLE TEST ===")
        for caso, ok in resultados:
            print(f"{caso}: {'OK' if ok else 'FALLO'}")

        aprobados = sum(1 for _, ok in resultados if ok)
        print(f"Casos rechazados correctamente: {aprobados}/{len(resultados)}")
        if aprobados == len(resultados):
            print("RESULTADO: el motor rechaza correctamente los escenarios imposibles.")
            return 0
        print("RESULTADO: hay fallos en el manejo de escenarios imposibles.")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
