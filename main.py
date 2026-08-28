from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from database import get_db_connection
from solver import optimizar_horarios_institucion  # <--- Importamos el solver

app = FastAPI(title="Chronos IA - Motor de Optimización", version="1.0.0")

class GenerarHorarioRequest(BaseModel):
    institucion_id: int
    periodo_lectivo_id: int

@app.post("/api/engine/generar")
def ejecutar_motor(payload: GenerarHorarioRequest):
    conn = None
    try:
        conn = get_db_connection()
        
        # Ejecutamos el motor de optimización matemática
        resultado = optimizar_horarios_institucion(
            payload.institucion_id, 
            payload.periodo_lectivo_id, 
            conn
        )

        return {
            "success": True,
            "message": "Horarios generados y optimizados sin cruces con éxito",
            "data": resultado
        }

    except Exception as e:
        if conn:
            conn.close()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if conn:
            conn.close()