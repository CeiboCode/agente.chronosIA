import random
from collections import defaultdict
from contextvars import ContextVar
from psycopg2.extras import RealDictCursor
import solver as solver_base

_periodo_contexto = ContextVar("periodo_optimizacion", default=None)

def _cargar_preferencias_periodo(cur, institucion_id, periodo_lectivo_id):
    defaults={"peso_huecos_docentes":40,"peso_huecos_grupos":25,"peso_balance_diario":20,"peso_distribucion_materias":15}
    cur.execute("SELECT peso_huecos_docentes,peso_huecos_grupos,peso_balance_diario,peso_distribucion_materias FROM preferencias_optimizacion_horarios WHERE institucion_id=%s AND periodo_lectivo_id=%s LIMIT 1",(institucion_id,periodo_lectivo_id))
    fila=cur.fetchone()
    if not fila:return defaults
    prefs={campo:int(fila[campo]) for campo in defaults}
    return prefs if sum(prefs.values())==100 else defaults

def _preferencias_contextuales(cur,institucion_id):
    periodo_id=_periodo_contexto.get()
    if periodo_id is None:raise ValueError("No se pudo determinar el período para cargar preferencias de optimización.")
    return _cargar_preferencias_periodo(cur,institucion_id,periodo_id)

solver_base._cargar_preferencias_optimizacion=_preferencias_contextuales
from solver import _cargar_asignaciones,_cargar_slots,_evaluar_calidad_horario,_validar_resultado_final
from solver_heuristic import optimizar_horarios_institucion as _optimizar_base
from solver_precheck import validar_capacidad_docentes
MAX_INTERCAMBIOS=350;MAX_SIN_MEJORA=120;OBJETIVO_CALIDAD=80.0

def _cargar_contexto_calidad(conn,i,p):
    cur=conn.cursor(cursor_factory=RealDictCursor)
    try:return _cargar_asignaciones(cur,i,p),_cargar_slots(cur,i,p),_cargar_preferencias_periodo(cur,i,p)
    finally:cur.close()

def _capturar_horario(conn,i,p):
    with conn.cursor() as cur:
        cur.execute("SELECT asignacion_carga_id,aula_id,bloque_tiempo_id,dia_indice FROM horarios WHERE institucion_id=%s AND periodo_lectivo_id=%s ORDER BY id_horario",(i,p))
        return [(i,p,int(a),int(au),int(b),int(d)) for a,au,b,d in cur.fetchall()]

def _guardar_horario(conn,i,p,horario):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM horarios WHERE institucion_id=%s AND periodo_lectivo_id=%s",(i,p))
        if horario:
            args=b",".join(cur.mogrify("(%s,%s,%s,%s,%s,%s)",x) for x in horario)
            cur.execute(b"INSERT INTO horarios (institucion_id,periodo_lectivo_id,asignacion_carga_id,aula_id,bloque_tiempo_id,dia_indice) VALUES "+args)
    conn.commit()

def _optimizar_intercambios(i,p,asignaciones,slots,prefs,inicial):
    por_id={int(a["id_asignacion_carga"]):a for a in asignaciones};indices=defaultdict(list)
    for n,item in enumerate(inicial):
        a=por_id[int(item[2])];indices[(int(a["curso_id"]),int(a["paralelo_id"]))].append(n)
    grupos=[x for x in indices.values() if len(x)>=2]
    if not grupos:return inicial,_evaluar_calidad_horario(asignaciones,slots,inicial,prefs),0,0
    mejor=list(inicial);calidad=_evaluar_calidad_horario(asignaciones,slots,mejor,prefs);puntaje=float(calidad["puntaje"]);mejoras=intentos=sin=0
    while intentos<MAX_INTERCAMBIOS and sin<MAX_SIN_MEJORA:
        if puntaje>=OBJETIVO_CALIDAD:break
        intentos+=1;i1,i2=random.sample(random.choice(grupos),2);cand=list(mejor);x,y=cand[i1],cand[i2]
        cand[i1]=(x[0],x[1],x[2],x[3],y[4],y[5]);cand[i2]=(y[0],y[1],y[2],y[3],x[4],x[5])
        try:_validar_resultado_final(asignaciones,slots,cand)
        except ValueError:sin+=1;continue
        nueva=_evaluar_calidad_horario(asignaciones,slots,cand,prefs);np=float(nueva["puntaje"])
        if np>puntaje:mejor,calidad,puntaje=cand,nueva,np;mejoras+=1;sin=0
        else:sin+=1
    return mejor,calidad,mejoras,intentos

def optimizar_horarios_institucion(institucion_id:int,periodo_lectivo_id:int,conn):
    token=_periodo_contexto.set(periodo_lectivo_id)
    try:
        validar_capacidad_docentes(conn,institucion_id,periodo_lectivo_id)
        resultado=_optimizar_base(institucion_id,periodo_lectivo_id,conn);inicial=_capturar_horario(conn,institucion_id,periodo_lectivo_id);pi=float((resultado.get("calidad") or {}).get("puntaje",0.0))
        asignaciones,slots,prefs=_cargar_contexto_calidad(conn,institucion_id,periodo_lectivo_id);mejor,calidad,mejoras,intentos=_optimizar_intercambios(institucion_id,periodo_lectivo_id,asignaciones,slots,prefs,inicial);pf=float(calidad.get("puntaje",pi))
        if pf>pi:_guardar_horario(conn,institucion_id,periodo_lectivo_id,mejor);resultado["calidad"]=calidad
        resultado["optimizacion_calidad"]={"metodo":"heuristica_continuidad_balance_mas_intercambios","rondas_ejecutadas":1,"puntaje_inicial":pi,"puntaje_final":max(pi,pf),"puntajes_evaluados":[pi,max(pi,pf)],"intercambios_intentados":intentos,"mejoras_aceptadas":mejoras,"objetivo_calidad":OBJETIVO_CALIDAD}
        return resultado
    finally:_periodo_contexto.reset(token)
