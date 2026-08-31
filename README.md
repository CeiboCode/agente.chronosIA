# Chronos IA — Motor de Horarios

Motor de generación de horarios académicos de **Chronos IA**.

Este repositorio contiene el servicio desarrollado en **Python** y **FastAPI** encargado de construir horarios válidos a partir de la configuración académica almacenada en PostgreSQL.

El motor recibe solicitudes desde la API Node.js, consulta la información necesaria en la base de datos, valida restricciones y genera una solución compatible con docentes, cursos, paralelos, materias, turnos y carga académica.

## Responsabilidades

- Validar que una institución y un período tengan la configuración mínima necesaria.
- Leer la configuración académica desde PostgreSQL.
- Construir el problema de planificación.
- Detectar configuraciones imposibles antes de intentar generar.
- Evitar conflictos de docentes.
- Evitar conflictos de Curso + Paralelo.
- Respetar turnos y bloques disponibles.
- Respetar las horas semanales asignadas.
- Aplicar restricciones de consecutividad de materias cuando corresponda.
- Buscar una solución válida y evaluar su calidad.
- Retornar resultados y errores estructurados a la API Node.js.

## Stack

- Python 3
- FastAPI
- Uvicorn
- PostgreSQL
- psycopg2
- python-dotenv

Dependencias actuales:

```text
fastapi==0.110.0
uvicorn==0.28.0
psycopg2-binary==2.9.9
python-dotenv==1.0.1
```

## Arquitectura

```text
Angular Frontend
      |
      v
Node.js API
      |
      | IDs internos del período/institución
      v
Python / FastAPI
      |
      v
PostgreSQL
      |
      v
Generador de horarios
```

El frontend nunca se comunica directamente con este servicio. La API Node.js actúa como capa de integración y resuelve los UUID públicos antes de invocar al motor.

## Requisitos

- Python 3.10 o superior recomendado
- PostgreSQL accesible desde el servidor del motor
- `pip`
- entorno virtual recomendado

## Instalación

Crear un entorno virtual:

```bash
python -m venv .venv
```

Activarlo en Linux/macOS:

```bash
source .venv/bin/activate
```

En Windows:

```powershell
.venv\Scripts\activate
```

Instalar dependencias:

```bash
pip install -r requirements.txt
```

## Configuración

Copia el archivo de ejemplo:

```bash
cp .env.example .env
```

Configuración base:

```env
DB_HOST=127.0.0.1
DB_PORT=5432
DB_NAME=chronos_ia_db
DB_USER=chronos_ia
DB_PASSWORD=cambia_esta_clave
DB_CONNECT_TIMEOUT=10
```

El servidor del motor debe poder conectarse directamente a PostgreSQL.

## Ejecución local

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Servicio local:

```text
http://127.0.0.1:8000
```

## Endpoints principales

### Health check

```http
GET /health
```

Permite comprobar que el servicio está disponible.

### Generar horario

```http
POST /api/engine/generar
```

La solicitud es realizada por la API Node.js utilizando los identificadores internos necesarios para cargar la configuración del período.

Cuando la configuración es imposible, el motor devuelve un error controlado que la API transforma en una respuesta adecuada para el frontend.

## Flujo interno

```text
Solicitud de generación
      |
      v
Carga de datos desde PostgreSQL
      |
      v
Prevalidaciones
      |
      +---- configuración imposible -> error controlado
      |
      v
Construcción del problema
      |
      v
Búsqueda de solución
      |
      v
Validación final
      |
      v
Evaluación de calidad
      |
      v
Respuesta a Node.js
```

## Restricciones académicas

El motor contempla, entre otras, las siguientes reglas:

- un docente no puede estar en dos clases al mismo tiempo;
- un Curso + Paralelo no puede tener dos clases simultáneas;
- las asignaciones deben cumplir sus horas semanales;
- las clases deben ubicarse dentro de bloques académicos válidos;
- se deben respetar los turnos configurados;
- los recreos no pueden utilizarse como clases;
- las materias pueden limitar horas consecutivas;
- la configuración debe ser factible antes de iniciar la búsqueda.

## Rendimiento

El motor incluye prevalidaciones y heurísticas para evitar trabajo innecesario y detectar rápidamente casos que no pueden generar un horario válido.

El objetivo principal es siempre producir un horario correcto antes de optimizar criterios secundarios de calidad.

## Producción

Dominio previsto:

```text
https://engine.chronosia.ceibocode.com
```

En producción se recomienda ejecutar Uvicorn mediante un servicio administrado por `systemd`, Supervisor u otra herramienta equivalente, detrás de Nginx con HTTPS.

Ejemplo básico:

```bash
uvicorn main:app --host 127.0.0.1 --port 8000
```

El puerto puede mantenerse privado y exponerse únicamente a través de Nginx o de la red interna entre VPS.

## Seguridad

- No expongas credenciales PostgreSQL en Git.
- Mantén el acceso al motor restringido a la API cuando sea posible.
- Utiliza firewall entre servidores.
- Usa HTTPS para tráfico público.
- La base de datos debe aceptar conexiones únicamente desde hosts autorizados.

## Repositorios relacionados

- `CeiboCode/api.chronosIA` — API Node.js que invoca al motor.
- `CeiboCode/chronosIA_app` — frontend Angular de Chronos IA.

## Proyecto

Chronos IA es desarrollado por **CeiboCode** para automatizar y optimizar la planificación de horarios académicos.