import csv
import os
import re
from datetime import datetime, timedelta
from urllib.parse import quote
from playwright.sync_api import sync_playwright

INSTANCE_URL = "https://coppelopsprod.service-now.com"
TABLE_NAME = "incident"
INPUT_CSV = "correos.csv"

# Obtener el rango de fechas de la semana pasada (Lunes a Domingo) para el nombre de la carpeta
MESES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"
]
ahora = datetime.now()
inicio_semana_actual = ahora - timedelta(days=ahora.weekday())
inicio_semana_pasada = inicio_semana_actual - timedelta(days=7)
fin_semana_pasada = inicio_semana_pasada + timedelta(days=6)
num_semana = inicio_semana_pasada.isocalendar()[1]

nombre_carpeta = (
    f"Reportes_Semana_{num_semana:02d}_"
    f"({inicio_semana_pasada.day:02d}_{MESES[inicio_semana_pasada.month - 1]}_"
    f"al_{fin_semana_pasada.day:02d}_{MESES[fin_semana_pasada.month - 1]}_{fin_semana_pasada.year})"
)
os.makedirs(nombre_carpeta, exist_ok=True)

# 1. Leer correos y equipo (si existe columna equipo) desde correos.csv
colaboradores = []
patron_correo = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

if os.path.exists(INPUT_CSV):
    with open(INPUT_CSV, mode="r", encoding="utf-8-sig", errors="ignore") as f:
        muestra = f.read(1024)
        f.seek(0)
        delimitador = ";" if ";" in muestra else ","
        f_csv = csv.reader(f, delimiter=delimitador)

        encabezados = []
        for idx, fila in enumerate(f_csv):
            if not fila:
                continue
            celdas = [c.strip(' "\'') for c in fila]

            # Detectar si la primera fila es encabezado
            if idx == 0 and any(h.lower() in ["correo", "email", "equipo", "celula"] for h in celdas):
                encabezados = [h.lower() for h in celdas]
                continue

            correo = ""
            equipo = "Sin Equipo"
            nombre = ""

            if encabezados:
                fila_dict = dict(zip(encabezados, celdas))
                correo = fila_dict.get("correo", fila_dict.get("email", ""))
                equipo = fila_dict.get("equipo", fila_dict.get("celula", "Sin Equipo")) or "Sin Equipo"
                nombre = fila_dict.get("nombre", "")
            else:
                for c in celdas:
                    if patron_correo.match(c):
                        correo = c
                    elif c and "@" not in c and equipo == "Sin Equipo":
                        equipo = c

            if patron_correo.match(correo):
                colaboradores.append({
                    "equipo": equipo,
                    "nombre": nombre,
                    "correo": correo
                })
else:
    print(f"Error: No se encontró el archivo '{INPUT_CSV}'.")
    exit(1)

if not colaboradores:
    print(f"No se encontraron correos válidos en '{INPUT_CSV}'.")
    exit(1)

# 2. Campos necesarios según reglas_auditoria.json + sys_id para consultar historial de auditoría
CAMPOS_AUDITORIA = [
    "number",
    "state",
    "active",
    "short_description",
    "description",
    "caller_id",
    "opened_by",
    "assigned_to",
    "assignment_group",
    "priority",
    "impact",
    "urgency",
    "severity",
    "u_estado_de_la_actividad",
    "business_service",
    "u_area",
    "u_vertical",
    "category",
    "subcategory",
    "u_subcategory_2",
    "u_subcategory_3",
    "u_subcategory_4",
    "made_sla",
    "business_duration",
    "problem_id",
    "parent_incident",
    "child_incidents",
    "origin_id",
    "reassignment_count",
    "reopen_count",
    "sys_created_on",
    "opened_at",
    "resolved_at",
    "closed_at",
    "close_code",
    "close_notes",
    "cause",
    "comments_and_work_notes"
]

fields_param = ",".join(["sys_id"] + CAMPOS_AUDITORIA)


def consultar_history_line(context, headers, sys_id_incidente, sys_id_agente, nombre_agente):
    """Consulta sys_history_line buscando la asignación al agente actual."""
    hist_query = f"set.id={sys_id_incidente}^field=assigned_to^ORDERBYDESCupdate_time"
    hist_url = (
        f"{INSTANCE_URL}/api/now/table/sys_history_line"
        f"?sysparm_query={quote(hist_query)}"
        f"&sysparm_fields=new,new_value,update_time"
        f"&sysparm_display_value=all"
        f"&sysparm_limit=20"
    )
    resp_hist = context.request.get(hist_url, headers=headers)
    if resp_hist.status == 200:
        lineas = resp_hist.json().get("result", [])
        for linea in lineas:
            new_disp = (
                linea.get("new", {}).get("display_value", "")
                if isinstance(linea.get("new"), dict)
                else str(linea.get("new", ""))
            )
            new_val = (
                linea.get("new_value", {}).get("value", "")
                if isinstance(linea.get("new_value"), dict)
                else str(linea.get("new_value", ""))
            )
            if (
                (nombre_agente and nombre_agente.lower() in new_disp.lower())
                or (sys_id_agente and new_val == sys_id_agente)
                or (len(lineas) == 1 and new_disp.strip() != "")
            ):
                upd_obj = linea.get("update_time", {})
                if isinstance(upd_obj, dict):
                    return upd_obj.get("display_value", "") or upd_obj.get("value", "")
                return str(upd_obj)
    return ""


def obtener_fecha_asignacion_agente(context, page_aux, headers, sys_id_incidente, sys_id_agente, nombre_agente):
    """
    Obtiene la fecha y hora exacta en que el ticket fue asignado al agente actual:
    1) Consulta directa a sys_history_line.
    2) Si está purgado/vacío, solicita la vista de historial de ServiceNow para reconstruir
       el sys_history_set en el servidor y vuelve a leer sys_history_line.
    3) Respaldo en sys_audit.
    """
    if not sys_id_incidente:
        return ""

    # Paso 1: Intentar leer directamente de sys_history_line
    fecha = consultar_history_line(context, headers, sys_id_incidente, sys_id_agente, nombre_agente)
    if fecha:
        return fecha

    # Paso 2: Forzar reconstrucción del historial en ServiceNow abriendo el endpoint de historial del registro
    try:
        # Llamada ligera al formulario clásico/historial que dispara la regeneración de sys_history_set
        context.request.get(
            f"{INSTANCE_URL}/sys_history_set.do?sysparm_query=id={sys_id_incidente}&sysparm_target=incident",
            headers=headers
        )
        fecha = consultar_history_line(context, headers, sys_id_incidente, sys_id_agente, nombre_agente)
        if fecha:
            return fecha

        # Si aún no se regeneró, cargar la vista de historial en la pestaña auxiliar y leer el DOM/API
        page_aux.goto(
            f"{INSTANCE_URL}/incident.do?sys_id={sys_id_incidente}&sysparm_view=sys_popup",
            wait_until="domcontentloaded",
            timeout=15000
        )
        fecha = consultar_history_line(context, headers, sys_id_incidente, sys_id_agente, nombre_agente)
        if fecha:
            return fecha
    except Exception:
        pass

    # Paso 3: Respaldo en sys_audit
    audit_query = f"documentkey={sys_id_incidente}^fieldname=assigned_to^ORDERBYDESCsys_created_on"
    audit_url = (
        f"{INSTANCE_URL}/api/now/table/sys_audit"
        f"?sysparm_query={quote(audit_query)}"
        f"&sysparm_fields=newvalue,sys_created_on"
        f"&sysparm_display_value=all"
        f"&sysparm_limit=20"
    )
    resp_audit = context.request.get(audit_url, headers=headers)
    if resp_audit.status == 200:
        registros = resp_audit.json().get("result", [])
        for reg in registros:
            new_val_obj = reg.get("newvalue", {})
            raw_val = new_val_obj.get("value", "") if isinstance(new_val_obj, dict) else str(new_val_obj)
            disp_val = new_val_obj.get("display_value", "") if isinstance(new_val_obj, dict) else str(new_val_obj)

            if (sys_id_agente and raw_val == sys_id_agente) or (
                nombre_agente and nombre_agente.lower() in disp_val.lower()
            ):
                created_obj = reg.get("sys_created_on", {})
                if isinstance(created_obj, dict):
                    return created_obj.get("display_value", "") or created_obj.get("value", "")
                return str(created_obj)

    return ""


with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://localhost:9222")
    context = browser.contexts[0]

    page = None
    for p_tab in context.pages:
        if "service-now.com" in p_tab.url:
            page = p_tab
            break

    if not page:
        page = context.new_page()
        page.goto(INSTANCE_URL)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(3000)

    # Pestaña auxiliar para reconstruir el historial solo en los tickets donde sys_history_line esté purgado
    page_aux = context.new_page()

    user_token = page.evaluate("() => typeof g_ck !== 'undefined' ? g_ck : ''")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    if user_token:
        headers["X-UserToken"] = user_token

    print(f"--- Iniciando extracción limpia para {len(colaboradores)} usuarios ---")
    print(f"Carpeta de destino: ./{nombre_carpeta}/\n")

    ini_dia = inicio_semana_pasada.strftime("%Y-%m-%d")
    fin_dia = fin_semana_pasada.strftime("%Y-%m-%d")
    backlog_dia = (ahora - timedelta(days=21)).strftime("%Y-%m-%d")

    for colab in colaboradores:
        correo = colab["correo"]
        equipo = colab["equipo"]
        print(f"Consultando incidentes de [{equipo}] -> {correo}...")

        query = (
            f"assigned_to.email={correo}^resolved_atBETWEENjavascript:gs.dateGenerate('{ini_dia}','00:00:00')@javascript:gs.dateGenerate('{fin_dia}','23:59:59')"
            f"^NQ"
            f"assigned_to.email={correo}^closed_atBETWEENjavascript:gs.dateGenerate('{ini_dia}','00:00:00')@javascript:gs.dateGenerate('{fin_dia}','23:59:59')"
            f"^NQ"
            f"assigned_to.email={correo}^active=true^sys_created_on<javascript:gs.dateGenerate('{backlog_dia}','00:00:00')"
        )

        api_url = (
            f"{INSTANCE_URL}/api/now/table/{TABLE_NAME}"
            f"?sysparm_query={quote(query)}"
            f"&sysparm_fields={fields_param}"
            f"&sysparm_display_value=all"
            f"&sysparm_limit=1000"
        )

        response = context.request.get(api_url, headers=headers)

        if response.status == 200:
            incidents = response.json().get("result", [])
            nombre_archivo_limpio = correo.replace("@", "_at_").replace(".", "_")
            ruta_salida = os.path.join(nombre_carpeta, f"{nombre_archivo_limpio}.csv")

            if incidents:
                columnas_finales = ["equipo_auditoria", "fecha_asignacion_agente"] + CAMPOS_AUDITORIA

                with open(ruta_salida, mode="w", newline="", encoding="utf-8-sig") as csv_file:
                    writer = csv.DictWriter(csv_file, fieldnames=columnas_finales)
                    writer.writeheader()

                    for inc in incidents:
                        row = {"equipo_auditoria": equipo}

                        sys_id_obj = inc.get("sys_id", {})
                        sys_id_inc = sys_id_obj.get("value", "") if isinstance(sys_id_obj, dict) else str(sys_id_obj)

                        assigned_obj = inc.get("assigned_to", {})
                        sys_id_agente = assigned_obj.get("value", "") if isinstance(assigned_obj, dict) else ""
                        nombre_agente = (
                            assigned_obj.get("display_value", "")
                            if isinstance(assigned_obj, dict)
                            else str(assigned_obj)
                        )

                        for field in CAMPOS_AUDITORIA:
                            val = inc.get(field, "")
                            if isinstance(val, dict):
                                val = val.get("display_value", "")
                            if isinstance(val, str):
                                val = val.replace("\r\n", "\n").strip()
                            row[field] = val

                        row["fecha_asignacion_agente"] = obtener_fecha_asignacion_agente(
                            context,
                            page_aux,
                            headers,
                            sys_id_inc,
                            sys_id_agente,
                            nombre_agente
                        )

                        writer.writerow(row)

                print(f"  -> Guardados {len(incidents)} registros limpios en: {ruta_salida}")
            else:
                print("  -> 0 incidentes encontrados.")
        else:
            print(f"  -> Error al consultar {correo}: Código {response.status}")

    page_aux.close()
    print("\nProceso finalizado con éxito.")