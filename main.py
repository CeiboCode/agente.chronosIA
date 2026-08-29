from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from database import get_db_connection
from solver import optimizar_horarios_institucion

app = FastAPI(
    title="Chronos IA - Motor de Optimización",
    version="1.1.0",
)


class GenerarHorarioRequest(BaseModel):
    institucion_id: int = Field(gt=0)
    periodo_lectivo_id: int = Field(gt=0)


@app.get("/health")
def health():
    return {
        "success": True,
        "service": "chronos-ia-engine",
        "version": "1.1.0",
    }


@app.post("/api/engine/generar")
def ejecutar_motor(payload: GenerarHorarioRequest):
    conn = None

    try:
        conn = get_db_connection()

        resultado = optimizar_horarios_institucion(
            payload.institucion_id,
            payload.periodo_lectivo_id,
            conn,
        )

        return {
            "success": True,
            "message": "Horario generado correctamente sin cruces de docentes ni cursos/paralelos.",
            "data": resultado,
        }

    except ValueError as e:
        if conn:
            conn.rollback()

        raise HTTPException(
            status_code=409,
            detail={
                "success": False,
                "code": "HORARIO_NO_GENERABLE",
                "message": str(e),
            },
        ) from e

    except Exception as e:
        if conn:
            conn.rollback()

        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "code": "ENGINE_ERROR",
                "message": "Error interno del motor de horarios.",
                "error": str(e),
            },
        ) from e

    finally:
        if conn:
            conn.close()
