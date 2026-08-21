import os
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import base64
from io import BytesIO
from weasyprint import HTML
from influxdb_client import InfluxDBClient
import smtplib
from email.message import EmailMessage
from datetime import datetime, time

# ==========================================
# 0. CONFIGURACIÓN DEL SISTEMA
# ==========================================
INFLUX_URL = "https://us-east-1-1.aws.cloud2.influxdata.com"
INFLUX_ORG = "7fc68e2daf710d5f"
INFLUX_BUCKET = "datalogger"

INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN") 
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD") 

EMAIL_SENDER = "monitoreoambienteucin@gmail.com" 
EMAIL_RECEIVER = "monitoreoambienteucin@gmail.com" 

# ==========================================
# FUNCIONES AUXILIARES GRÁFICAS Y DATOS
# ==========================================
def get_image_base64(filepath):
    try:
        with open(filepath, "rb") as f:
            return base64.b64encode(f.read()).decode('utf-8')
    except FileNotFoundError:
        return ""

def generar_graficos_diarios(df_dia):
    # Se crean 3 subgráficos: LAeq, LAF y Luz
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(9.5, 6.0), sharex=True)
    fig.subplots_adjust(hspace=0.25)
    
    if df_dia.empty:
        plt.close(fig)
        return ""

    tiempos = df_dia.index
    ruido_eq = df_dia['ruido_eq_dba'].values
    ruido_fast = df_dia['ruido_fast_dba'].values
    luz = df_dia['luz_lux'].values
    
    fecha_actual = tiempos[0].date()
    t_start = pd.Timestamp(datetime.combine(fecha_actual, time(0, 0)))
    t_end = pd.Timestamp(datetime.combine(fecha_actual, time(23, 59, 59)))
    
    t_day_start = pd.Timestamp(datetime.combine(fecha_actual, time(8, 0)))
    t_day_end = pd.Timestamp(datetime.combine(fecha_actual, time(20, 0)))

    # --- GRÁFICO 1: RUIDO CONTINUO (LAeq) ---
    ax1.plot(tiempos, ruido_eq, color='#2980b9', linewidth=1.2)
    ax1.axhline(45, color='#e74c3c', linestyle='--', linewidth=1, label='Límite (45 dBA)')
    ax1.axvspan(t_start, t_day_start, color='#2c3e50', alpha=0.08)
    ax1.axvspan(t_day_end, t_end, color='#2c3e50', alpha=0.08)
    
    pico_max_eq = np.nanmax(ruido_eq) if not np.isnan(ruido_eq).all() else 65
    ax1.set_ylim(30, max(85, pico_max_eq + 10))
    ax1.set_xlim(t_start, t_end)
    ax1.set_ylabel('LAeq (dBA)\nExposición', color='#2980b9', fontweight='bold', fontsize=9)
    ax1.grid(True, alpha=0.3)
    
    # --- GRÁFICO 2: PICOS INSTANTÁNEOS (LAF) ---
    ax2.plot(tiempos, ruido_fast, color='#8e44ad', linewidth=0.8, alpha=0.85)
    # Línea punteada en 65 dBA según AAP para eventos impulsivos
    ax2.axhline(65, color='#c0392b', linestyle=':', linewidth=1.5, label='Límite Picos (65 dBA)')
    ax2.axvspan(t_start, t_day_start, color='#2c3e50', alpha=0.08)
    ax2.axvspan(t_day_end, t_end, color='#2c3e50', alpha=0.08)
    
    pico_max_fast = np.nanmax(ruido_fast) if not np.isnan(ruido_fast).all() else 75
    ax2.set_ylim(30, max(90, pico_max_fast + 10))
    ax2.set_xlim(t_start, t_end)
    ax2.set_ylabel('LAF (dBA)\nPicos', color='#8e44ad', fontweight='bold', fontsize=9)
    ax2.grid(True, alpha=0.3)
    
    # --- GRÁFICO 3: LUZ ---
    ax3.plot(tiempos, luz, color='#f39c12', linewidth=1.2)
    ax3.fill_between(tiempos, luz, color='#f39c12', alpha=0.2)
    ax3.axvspan(t_start, t_day_start, color='#2c3e50', alpha=0.08)
    ax3.axvspan(t_day_end, t_end, color='#2c3e50', alpha=0.08)

    ax3.set_ylabel('Luz (Lux)', color='#d68910', fontweight='bold', fontsize=9)
    ax3.set_ylim(0, 600) 
    ax3.set_xlim(t_start, t_end)
    ax3.grid(True, alpha=0.3)
    
    ax3.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax3.xaxis.set_major_locator(mdates.HourLocator(interval=2)) 
    plt.xticks(rotation=0)
    ax3.set_xlabel('Hora del día (Fondo gris = Período Nocturno)')
    
    buf = BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', dpi=120)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode('utf-8')

def obtener_alertas_laeq_60_5min(df_dia):
    """Detecta alertas críticas: LAeq > 60 dBA sostenido por 5 minutos o más."""
    alertas = []
    if 'ruido_eq_dba' not in df_dia.columns:
        return alertas

    is_over = df_dia['ruido_eq_dba'] > 60
    consecutive_groups = is_over.ne(is_over.shift()).cumsum()
    over_threshold_periods = df_dia[is_over].groupby(consecutive_groups)
    
    for _, period in over_threshold_periods:
        if len(period) < 2:
            continue
            
        duracion = period.index[-1] - period.index[0]
        minutos = duracion.total_seconds() / 60.0
        
        if minutos >= 5.0:
            max_val = period['ruido_eq_dba'].max()
            inicio = period.index[0].strftime("%H:%M")
            fin = period.index[-1].strftime("%H:%M")
            minutos_int = int(minutos)
            alertas.append(f"Alerta: <strong>{max_val:.1f} dBA</strong> sostenido durante {minutos_int} min. ({inicio} a {fin})")
            
    return alertas

# ==========================================
# 1. EXTRACCIÓN DE DATOS
# ==========================================
def obtener_datos_influx():
    print("Conectando a InfluxDB Cloud...")
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    query_api = client.query_api()

    query = f'''
        from(bucket: "{INFLUX_BUCKET}")
        |> range(start: -7d)
        |> filter(fn: (r) => r["_measurement"] == "environment_data")
        |> filter(fn: (r) => r["_field"] == "node_1_laeq_1s_dba" or r["_field"] == "node_2_laeq_1s_dba" or r["_field"] == "node_1_laf_dba" or r["_field"] == "node_2_laf_dba" or r["_field"] == "lux")
        |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''
    
    df = query_api.query_data_frame(query)
    client.close()
    
    if not df.empty:
        df['_time'] = pd.to_datetime(df['_time']).dt.tz_convert('America/Argentina/Cordoba').dt.tz_localize(None)
        df.set_index('_time', inplace=True)
        df = df.sort_index()
        
        # LAeq (Para gráficos y exposición sostenida)
        if 'node_1_laeq_1s_dba' in df.columns and 'node_2_laeq_1s_dba' in df.columns:
            df['ruido_eq_dba'] = df[['node_1_laeq_1s_dba', 'node_2_laeq_1s_dba']].max(axis=1)
        elif 'node_1_laeq_1s_dba' in df.columns:
            df['ruido_eq_dba'] = df['node_1_laeq_1s_dba']
        elif 'node_2_laeq_1s_dba' in df.columns:
            df['ruido_eq_dba'] = df['node_2_laeq_1s_dba']
        else:
            df['ruido_eq_dba'] = np.nan
            
        # LAF (Para picos e impactos instantáneos)
        if 'node_1_laf_dba' in df.columns and 'node_2_laf_dba' in df.columns:
            df['ruido_fast_dba'] = df[['node_1_laf_dba', 'node_2_laf_dba']].max(axis=1)
        elif 'node_1_laf_dba' in df.columns:
            df['ruido_fast_dba'] = df['node_1_laf_dba']
        elif 'node_2_laf_dba' in df.columns:
            df['ruido_fast_dba'] = df['node_2_laf_dba']
        else:
            df['ruido_fast_dba'] = np.nan
            
        if 'lux' in df.columns:
            df.rename(columns={'lux': 'luz_lux'}, inplace=True)
        else:
            df['luz_lux'] = np.nan
            
        df.ffill(inplace=True) 
    
    return df

# ==========================================
# 2. PROCESAMIENTO Y GENERACIÓN DEL PDF
# ==========================================
def generar_pdf(df, ruta_salida="informe_semanal_ucin.pdf"):
    print("Procesando métricas y generando el PDF...")
    
    logo_hosp_b64 = get_image_base64("logo hospital.jpeg")
    logo_inst_b64 = get_image_base64("logos institucionales.jpeg")
    
    fecha_inicio = df.index.min().strftime("%d/%m/%Y")
    fecha_fin = df.index.max().strftime("%d/%m/%Y")
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
    <meta charset="UTF-8">
    <style>
        @page {{ size: A4; margin: 15mm 15mm; background-color: #ffffff; }}
        body {{ font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; color: #2c3e50; font-size: 10pt; line-height: 1.4; margin: 0; padding: 0; }}
        .header-table {{ width: 100%; border-bottom: 2px solid #2980b9; margin-bottom: 20px; padding-bottom: 10px; }}
        .header-table td {{ vertical-align: middle; border: none; }}
        h1 {{ color: #2c3e50; font-size: 16pt; text-align: center; margin: 0 0 5px 0; text-transform: uppercase; }}
        .metadata {{ text-align: center; font-size: 10pt; color: #7f8c8d; }}
        h2 {{ color: #2980b9; font-size: 14pt; border-bottom: 1px solid #bdc3c7; padding-bottom: 4px; margin-top: 25px; margin-bottom: 15px; page-break-after: avoid; }}
        .status-panel {{ display: table; width: 100%; margin-bottom: 15px; }}
        .status-box {{ display: table-cell; padding: 15px; background-color: #fff8e1; border-left: 5px solid #f39c12; }}
        .status-box.ok {{ background-color: #e8fdf0; border-left: 5px solid #27ae60; }}
        .status-box.alert {{ background-color: #fce8e6; border-left: 5px solid #c0392b; }}
        ul.alerts {{ margin: 5px 0 0 0; padding-left: 20px; color: #c0392b; font-size: 9.5pt; }}
        table.data-table {{ width: 100%; border-collapse: collapse; margin-bottom: 15px; font-size: 9.5pt; }}
        table.data-table th, table.data-table td {{ border: 1px solid #ecf0f1; padding: 8px; text-align: center; }}
        table.data-table th {{ background-color: #f4f7f6; color: #34495e; font-weight: bold; }}
        .day-block {{ page-break-inside: avoid; margin-bottom: 30px; background-color: #fafbfc; border: 1px solid #e1e4e8; padding: 15px; border-radius: 4px; }}
        .day-title {{ font-size: 12pt; font-weight: bold; color: #2c3e50; margin-bottom: 10px; text-transform: uppercase; border-bottom: 2px solid #2980b9; display: inline-block; padding-bottom: 2px; }}
        .metrics-container {{ display: table; width: 100%; margin-bottom: 10px; }}
        .metric-col {{ display: table-cell; width: 50%; padding-right: 10px; }}
        .metric-col:last-child {{ padding-right: 0; padding-left: 10px; border-left: 1px solid #ddd; }}
        .plot-img {{ width: 100%; max-width: 100%; height: auto; display: block; margin: 10px auto; }}
        .sustained-peaks {{ background-color: #fce8e6; border-left: 3px solid #e74c3c; padding: 10px; margin-bottom: 10px; font-size: 9pt; border-radius: 4px; }}
        .sustained-peaks strong {{ color: #c0392b; }}
        .alert-title {{ font-weight: bold; color: #c0392b; margin-bottom: 3px; display: block; }}
        .ref-text {{ font-size: 8.5pt; color: #7f8c8d; font-style: italic; margin-top: 5px; }}
    </style>
    </head>
    <body>
    <table class="header-table">
        <tr>
            <td style="width: 25%; text-align: left;"><img src="data:image/jpeg;base64,{logo_hosp_b64}" style="max-height: 90px; max-width: 100%;"></td>
            <td style="width: 50%; text-align: center;">
                <h1>Informe Semanal de Monitoreo Ambiental</h1>
                <div class="metadata"><strong>UCIN</strong><br>Semana: {fecha_inicio} al {fecha_fin}</div>
            </td>
            <td style="width: 25%; text-align: right;"><img src="data:image/jpeg;base64,{logo_inst_b64}" style="max-height: 90px; max-width: 100%; float: right;"></td>
        </tr>
    </table>
    """
    
    daily_blocks = []
    total_ruido = []
    
    dias_esp = {"Monday": "LUNES", "Tuesday": "MARTES", "Wednesday": "MIÉRCOLES", "Thursday": "JUEVES", "Friday": "VIERNES", "Saturday": "SÁBADO", "Sunday": "DOMINGO"}
    
    for date, group in df.groupby(df.index.date):
        if group.empty: continue
            
        day_name = dias_esp.get(date.strftime("%A"), "")
        date_str = date.strftime("%d/%m/%Y")
        
        horas = group.index.hour
        mask_day = (horas >= 8) & (horas < 20)
        mask_night = ~mask_day
        
        df_diurno = group[mask_day]
        df_nocturno = group[mask_night]
        
        ruido_eq = group['ruido_eq_dba'].dropna().values
        luz = group['luz_lux'].dropna().values
        
        if len(ruido_eq) == 0 or len(luz) == 0: continue
            
        r_diurno = df_diurno['ruido_eq_dba'].mean() if not df_diurno.empty else 0
        r_nocturno = df_nocturno['ruido_eq_dba'].mean() if not df_nocturno.empty else 0
        l_diurna = df_diurno['luz_lux'].mean() if not df_diurno.empty else 0
        l_nocturna = df_nocturno['luz_lux'].mean() if not df_nocturno.empty else 0
        
        total_ruido.append(np.mean(ruido_eq))
        
        # Obtenemos las alertas de ruido continuo crítico para este día
        alertas_laeq = obtener_alertas_laeq_60_5min(group)
        pct_fuera_norma = (np.sum(ruido_eq > 45) / len(ruido_eq)) * 100
        
        img_graficos = generar_graficos_diarios(group)
        
        # Bloque de Alertas (Solo se imprime si hubo algún evento crítico)
        html_picos_sostenidos = ""
        if alertas_laeq:
            lista_laeq = "</li><li>".join(alertas_laeq)
            html_picos_sostenidos = f"""
            <div class="sustained-peaks">
                <span class="alert-title">⚠️ Alertas Críticas (LAeq > 60 dBA sostenido por ≥ 5 min):</span>
                <ul style="margin: 0; padding-left: 20px;"><li>{lista_laeq}</li></ul>
                <div class="ref-text">Referencia: La detección se activa al registrarse energía acústica constante superior a 60 dBA durante un bloque ininterrumpido de 5 minutos o más.</div>
            </div>
            """
        
        block = f"""
        <div class="day-block">
            <div class="day-title">{day_name} {date_str}</div>
            <div class="metrics-container">
                <div class="metric-col">
                    <strong>Análisis Acústico (LAeq)</strong>
                    <table class="data-table" style="margin-top: 5px;">
                        <tr><td>Promedio Diurno:</td><td>{r_diurno:.1f} dBA</td></tr>
                        <tr><td>Promedio Nocturno:</td><td>{r_nocturno:.1f} dBA</td></tr>
                        <tr><td>Exposición al ruido de fondo (>45 dBA):</td><td style="color: {'#e67e22' if pct_fuera_norma > 50 else '#34495e'};"><strong>{pct_fuera_norma:.1f}%</strong> del día</td></tr>
                    </table>
                </div>
                <div class="metric-col">
                    <strong>Análisis Lumínico</strong>
                    <table class="data-table" style="margin-top: 5px;">
                        <tr><td>Promedio Diurno:</td><td>{l_diurna:.0f} Lux</td></tr>
                        <tr><td>Promedio Nocturno:</td><td>{l_nocturna:.0f} Lux</td></tr>
                    </table>
                </div>
            </div>
            
            {html_picos_sostenidos}
            
            <div style="font-size: 8.5pt; color: #34495e; margin-bottom: 5px;">
                <strong>Guía de lectura:</strong> El panel superior (LAeq) muestra la carga acústica o exposición continua. El panel central (LAF) identifica picos o impactos instantáneos.
            </div>
            <img src="data:image/png;base64,{img_graficos}" class="plot-img">
        </div>
        """
        daily_blocks.append(block)

    # Estado global evaluado con el nuevo límite de 45 dBA
    promedio_semanal = np.mean(total_ruido) if total_ruido else 0
    if promedio_semanal <= 45:
        estado_global = "Las mediciones promedio de la semana se mantienen estables respecto a los umbrales de confort neonatal."
        status_class = "status-box ok"
    else:
        estado_global = f"El nivel promedio de la semana ({promedio_semanal:.1f} dBA) se encuentra por encima del umbral de confort recomendado."
        status_class = "status-box alert"

    html_content += f"""
    <h2>1- Resumen general de la semana</h2>
    <div class="status-panel">
        <div class="{status_class}">
            <strong style="font-size: 11pt;">Estado Acústico Global:</strong><br>
            {estado_global}<br>
            <span class="ref-text" style="display: block; margin-top: 8px;">(Según el límite de exposición continua establecido por la AAP: 45 dBA LAeq)</span>
        </div>
    </div>

    <h2>2- Análisis diario (ruido y luz)</h2>
    <p style="font-size: 9.5pt; color: #7f8c8d; margin-top: 0; margin-bottom: 20px;">
    <strong>Criterio de Segmentación:</strong> Los promedios se dividen en Período Diurno (08:00 a 20:00 hs) y Período Nocturno (20:00 a 08:00 hs). En las gráficas, el área con fondo gris delimita las horas nocturnas.
    </p>
    """

    for b in daily_blocks:
        html_content += b

    html_content += """
    <h2>3- Estado del sistema</h2>
    <table class="data-table">
        <tr><th style="width: 30%;">Métrica de Red</th><th style="width: 70%;">Estado / Desempeño</th></tr>
        <tr><td><strong>Conectividad y Uptime</strong></td><td>La red WiFi de interconexión operó de forma continua para la captura de las muestras expuestas.</td></tr>
    </table>
    </body>
    </html>
    """

    HTML(string=html_content).write_pdf(ruta_salida)
    print(f"PDF generado exitosamente en: {ruta_salida}")
    return ruta_salida

# ==========================================
# 3. ENVÍO DE CORREO AUTOMATIZADO
# ==========================================
def enviar_correo(ruta_pdf):
    if not EMAIL_PASSWORD:
        print("Advertencia: No se encontró la contraseña del correo.")
        return
        
    print("Preparando envío de correo...")
    msg = EmailMessage()
    msg['Subject'] = 'Informe Semanal UCIN - Monitoreo Ambiental (Automatizado)'
    msg['From'] = EMAIL_SENDER
    msg['To'] = EMAIL_RECEIVER
    msg.set_content('Estimado equipo,\n\nSe adjunta el informe semanal correspondiente a las mediciones acústicas y lumínicas de la sala, generado a partir de los registros de telemetría de los módulos ESP32.\n\nSaludos cordiales,\nSistema de Monitoreo UCIN')

    with open(ruta_pdf, 'rb') as f:
        msg.add_attachment(f.read(), maintype='application', subtype='pdf', filename="Informe_Semanal_UCIN.pdf")

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(EMAIL_SENDER, EMAIL_PASSWORD)
            smtp.send_message(msg)
        print("¡Correo enviado con éxito!")
    except Exception as e:
        print(f"Error al enviar el correo: {e}")

# ==========================================
# EJECUCIÓN PRINCIPAL
# ==========================================
if __name__ == "__main__":
    try:
        df_sensores = obtener_datos_influx()
        if not df_sensores.empty:
            pdf_generado = generar_pdf(df_sensores)
            enviar_correo(pdf_generado)
            print("--- Proceso Semanal Finalizado Correctamente ---")
        else:
            print("No se encontraron datos en InfluxDB para los últimos 7 días.")
    except Exception as e:
        print(f"Ocurrió un error crítico durante la ejecución: {e}")
