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

def generar_grafico_24h(df_dia):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9.5, 4.5), sharex=True)
    fig.subplots_adjust(hspace=0.2)
    
    if df_dia.empty:
        plt.close(fig)
        return ""

    tiempos = df_dia.index
    ruido = df_dia['ruido_dba'].values
    luz = df_dia['luz_lux'].values
    
    # Definir los límites del día (00:00 a 23:59)
    fecha_actual = tiempos[0].date()
    t_start = pd.Timestamp(datetime.combine(fecha_actual, time(0, 0)))
    t_end = pd.Timestamp(datetime.combine(fecha_actual, time(23, 59, 59)))
    
    # Definir los límites del turno diurno (08:00 a 20:00)
    t_day_start = pd.Timestamp(datetime.combine(fecha_actual, time(8, 0)))
    t_day_end = pd.Timestamp(datetime.combine(fecha_actual, time(20, 0)))

    # --- GRÁFICO DE RUIDO ---
    ax1.plot(tiempos, ruido, color='#2980b9', linewidth=1.2)
    ax1.axhline(45, color='#e74c3c', linestyle='--', linewidth=1, label='Límite (45 dBA)')
    
    # Sombreado Nocturno (Fondo Gris)
    ax1.axvspan(t_start, t_day_start, color='#2c3e50', alpha=0.08) # Madrugada
    ax1.axvspan(t_day_end, t_end, color='#2c3e50', alpha=0.08)     # Noche
    
    pico_max = np.nanmax(ruido) if not np.isnan(ruido).all() else 65
    ax1.set_ylim(30, max(85, pico_max + 10))
    ax1.set_xlim(t_start, t_end)
    ax1.set_ylabel('Ruido (dBA)', color='#2980b9', fontweight='bold')
    ax1.grid(True, alpha=0.3)
    
    # --- GRÁFICO DE LUZ ---
    ax2.plot(tiempos, luz, color='#f39c12', linewidth=1.2)
    ax2.fill_between(tiempos, luz, color='#f39c12', alpha=0.2)
    
    # Sombreado Nocturno (Fondo Gris)
    ax2.axvspan(t_start, t_day_start, color='#2c3e50', alpha=0.08)
    ax2.axvspan(t_day_end, t_end, color='#2c3e50', alpha=0.08)

    ax2.set_ylabel('Luz (Lux)', color='#d68910', fontweight='bold')
    ax2.set_ylim(0, 1000) 
    ax2.set_xlim(t_start, t_end)
    ax2.grid(True, alpha=0.3)
    
    # Eje X: Ticks cada 2 horas (00:00, 02:00, etc.) para que no se amontonen
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax2.xaxis.set_major_locator(mdates.HourLocator(interval=2)) 
    plt.xticks(rotation=0)
    ax2.set_xlabel('Hora del día (Fondo gris = Período Nocturno)')
    
    buf = BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', dpi=120)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode('utf-8')

def obtener_picos_sostenidos(df_dia):
    """Detecta períodos donde el ruido supera el umbral crítico (65 dBA) de forma continua"""
    is_over = df_dia['ruido_dba'] > 65
    consecutive_groups = is_over.ne(is_over.shift()).cumsum()
    over_threshold_periods = df_dia[is_over].groupby(consecutive_groups)
    
    alertas_sostenidas = []
    for _, period in over_threshold_periods:
        if len(period) < 2: 
            continue
            
        duracion = period.index[-1] - period.index[0]
        minutos = int(duracion.total_seconds() // 60)
        
        if minutos >= 1:
            max_val = period['ruido_dba'].max()
            inicio = period.index[0].strftime("%H:%M")
            fin = period.index[-1].strftime("%H:%M")
            alertas_sostenidas.append(f"Ruido crítico continuo (>65 dBA) por {minutos} min. Pico: <strong>{max_val:.1f} dBA</strong> ({inicio} a {fin})")
            
    return alertas_sostenidas

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
        |> filter(fn: (r) => r["_field"] == "node_1_laeq_1s_dba" or r["_field"] == "node_2_laeq_1s_dba" or r["_field"] == "lux")
        |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''
    
    df = query_api.query_data_frame(query)
    client.close()
    
    if not df.empty:
        df['_time'] = pd.to_datetime(df['_time']).dt.tz_convert('America/Argentina/Cordoba').dt.tz_localize(None)
        df.set_index('_time', inplace=True)
        df = df.sort_index()
        
        if 'node_1_laeq_1s_dba' in df.columns and 'node_2_laeq_1s_dba' in df.columns:
            df['ruido_dba'] = df[['node_1_laeq_1s_dba', 'node_2_laeq_1s_dba']].max(axis=1)
        elif 'node_1_laeq_1s_dba' in df.columns:
            df['ruido_dba'] = df['node_1_laeq_1s_dba']
        elif 'node_2_laeq_1s_dba' in df.columns:
            df['ruido_dba'] = df['node_2_laeq_1s_dba']
        else:
            df['ruido_dba'] = np.nan
            
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
        ul.alerts {{ margin: 5px 0 0 0; padding-left: 20px; color: #c0392b; font-size: 9.5pt; }}
        table.data-table {{ width: 100%; border-collapse: collapse; margin-bottom: 15px; font-size: 9.5pt; }}
        table.data-table th, table.data-table td {{ border: 1px solid #ecf0f1; padding: 8px; text-align: center; }}
        table.data-table th {{ background-color: #f4f7f6; color: #34495e; font-weight: bold; }}
        .day-block {{ page-break-inside: avoid; margin-bottom: 30px; background-color: #fafbfc; border: 1px solid #e1e4e8; padding: 15px; border-radius: 4px; }}
        .day-title {{ font-size: 12pt; font-weight: bold; color: #2c3e50; margin-bottom: 10px; text-transform: uppercase; border-bottom: 2px solid #2980b9; display: inline-block; padding-bottom: 2px; }}
        .metrics-container {{ display: table; width: 100%; margin-bottom: 15px; }}
        .metric-col {{ display: table-cell; width: 50%; padding-right: 10px; }}
        .metric-col:last-child {{ padding-right: 0; padding-left: 10px; border-left: 1px solid #ddd; }}
        .plot-img {{ width: 100%; max-width: 100%; height: auto; display: block; margin: 10px auto; }}
        .sustained-peaks {{ background-color: #fce8e6; border-left: 3px solid #e74c3c; padding: 8px; margin-top: 10px; font-size: 9pt; }}
        .sustained-peaks strong {{ color: #c0392b; }}
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
    alertas_generales = []
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
        
        ruido = group['ruido_dba'].dropna().values
        luz = group['luz_lux'].dropna().values
        
        if len(ruido) == 0 or len(luz) == 0: continue
            
        r_diurno = df_diurno['ruido_dba'].mean() if not df_diurno.empty else 0
        r_nocturno = df_nocturno['ruido_dba'].mean() if not df_nocturno.empty else 0
        l_diurna = df_diurno['luz_lux'].mean() if not df_diurno.empty else 0
        l_nocturna = df_nocturno['luz_lux'].mean() if not df_nocturno.empty else 0
        
        total_ruido.append(np.mean(ruido))
        
        picos_dia = obtener_picos_sostenidos(group)
        if picos_dia:
            for p in picos_dia:
                alertas_generales.append(f"<strong>{day_name} {date_str}:</strong> {p}")
        
        pct_fuera_norma = (np.sum(ruido > 45) / len(ruido)) * 100
        
        # Generamos UN SOLO GRÁFICO de 24 horas por día
        img_24h = generar_grafico_24h(group)
        
        html_picos_sostenidos = ""
        if picos_dia:
            lista_picos = "</li><li>".join(picos_dia)
            html_picos_sostenidos = f"""
            <div class="sustained-peaks">
                <strong>⚠️ Picos Máximos Sostenidos (>65 dBA por más de 1 min):</strong>
                <ul style="margin: 5px 0 0 0; padding-left: 20px;"><li>{lista_picos}</li></ul>
            </div>
            """
        
        block = f"""
        <div class="day-block">
            <div class="day-title">{day_name} {date_str}</div>
            <div class="metrics-container">
                <div class="metric-col">
                    <strong>Análisis Acústico</strong>
                    <table class="data-table" style="margin-top: 5px;">
                        <tr><td>Promedio Diurno:</td><td>{r_diurno:.1f} dBA</td></tr>
                        <tr><td>Promedio Nocturno:</td><td>{r_nocturno:.1f} dBA</td></tr>
                        <tr><td>Tiempo total > 45 dBA:</td><td style="color: {'#c0392b' if pct_fuera_norma > 20 else '#27ae60'};"><strong>{pct_fuera_norma:.1f}%</strong> del día</td></tr>
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
            
            <img src="data:image/png;base64,{img_24h}" class="plot-img">
        </div>
        """
        daily_blocks.append(block)

    alert_html = ""
    status_class = "status-box"
    if len(alertas_generales) > 0:
        alert_html = "<ul class='alerts'><li>" + "</li><li>".join(alertas_generales) + "</li></ul>"
    else:
        status_class += " ok"
        alert_html = "<span style='color: #27ae60; font-weight: bold;'>✓ Excelente: No se registraron eventos sostenidos de ruido por encima del límite crítico de tolerancia.</span>"

    html_content += f"""
    <h2>1- Resumen general de la semana</h2>
    <div class="status-panel">
        <div class="{status_class}">
            <strong style="font-size: 11pt;">Estado Acústico Global:</strong><br>
            Las mediciones promedio de la semana se mantienen {'estables' if np.mean(total_ruido) < 50 else 'con advertencias'} respecto a los umbrales de confort neonatal. 
            <br><br><strong>Alertas Críticas de la Semana (Picos > 65 dBA por más de 1 min):</strong><br>{alert_html}
        </div>
    </div>

    <h2>2- Análisis diario (ruido y luz)</h2>
    <p style="font-size: 9.5pt; color: #7f8c8d; margin-top: 0; margin-bottom: 20px;">
    <strong>Criterio de Segmentación Horaria:</strong> Los promedios se dividen en Período Diurno (08:00 a 20:00 hs) y Período Nocturno (20:00 a 08:00 hs). Las gráficas muestran las 24 horas continuas con un <strong>fondo gris</strong> indicando las horas nocturnas.
    </p>
    """

    for b in daily_blocks:
        html_content += b

    html_content += """
    <h2>3- Estado del sistema</h2>
    <table class="data-table">
        <tr><th style="width: 30%;">Métrica de Red</th><th style="width: 70%;">Estado / Desempeño</th></tr>
        <tr><td><strong>Conectividad y Uptime</strong></td><td>La red WiFi de interconexión (ESP_A ↔ ESP minis ↔ ESP_B display) operó de forma continua para la captura de las muestras expuestas.</td></tr>
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
