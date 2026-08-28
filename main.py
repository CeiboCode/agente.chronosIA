from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from database import get_db_connection
from solver import optimizar_horarios_institucion

app = FastAPI(
    title="Chronos IA - Motor de Optimización",
    version="1.0.0",
)


class GenerarHorarioRequest(BaseModel):
    institucion_id: int = Field(gt=0)
    periodo_lectivo_id: int = Field(gt=0)


@app.get("/health")
def health():
    return {
        "success": True,
        "service": "chronos-ia-engine",
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
            "message": "Horarios generados y optimizados sin cruces con éxito",
            "data": resultado,
        }

    except ValueError as e:
        if conn:
            conn.rollback()

        raise HTTPException(
            status_code=400,
            detail=str(e),
        ) from e

    except Exception as e:
        if conn:
            conn.rollback()

        raise HTTPException(
            status_code=500,
            detail=str(e),
        ) from e

    finally:
        if conn:
            conn.close()