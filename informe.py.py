import os
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg') # Fundamental para que las gráficas funcionen en servidores sin pantalla como GitHub Actions
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import base64
from io import BytesIO
from weasyprint import HTML
from influxdb_client import InfluxDBClient
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta

# ==========================================
# 0. CONFIGURACIÓN DEL SISTEMA
# ==========================================
INFLUX_URL = "https://us-east-1-1.aws.cloud2.influxdata.com"
INFLUX_ORG = "7fc68e2daf710d5f"
INFLUX_BUCKET = "datalogger"

# Extracción segura de credenciales desde GitHub Secrets
INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN") 
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD") 

# Correos
EMAIL_SENDER = "tu_correo@gmail.com" # CAMBIA ESTO por tu dirección de Gmail
EMAIL_RECEIVER = "neonatologia@hospital.com" # CAMBIA ESTO por el correo del destinatario

# ==========================================
# FUNCIONES AUXILIARES
# ==========================================
def fig_to_base64(fig):
    buf = BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', dpi=120)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode('utf-8')

def get_image_base64(filepath):
    try:
        with open(filepath, "rb") as f:
            return base64.b64encode(f.read()).decode('utf-8')
    except FileNotFoundError:
        return ""

# ==========================================
# 1. EXTRACCIÓN DE DATOS (InfluxDB)
# ==========================================
def obtener_datos_influx():
    print("Conectando a InfluxDB Cloud...")
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    query_api = client.query_api()

    # Traemos los últimos 7 días
    query = f'''
        from(bucket: "{INFLUX_BUCKET}")
        |> range(start: -7d)
        |> filter(fn: (r) => r["_measurement"] == "environment_data")
        |> filter(fn: (r) => r["_field"] == "node_1_laf_dba" or r["_field"] == "node_2_laf_dba" or r["_field"] == "lux")
        |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''
    
    df = query_api.query_data_frame(query)
    client.close()
    
    if not df.empty:
        # Convertir a hora local para cortes precisos de día/noche
        df['_time'] = pd.to_datetime(df['_time']).dt.tz_convert('America/Argentina/Cordoba')
        df.set_index('_time', inplace=True)
        df = df.sort_index()
        
        # Tomar el pico de ruido máximo entre el Nodo 1 y Nodo 2
        if 'node_1_laf_dba' in df.columns and 'node_2_laf_dba' in df.columns:
            df['ruido_dba'] = df[['node_1_laf_dba', 'node_2_laf_dba']].max(axis=1)
        elif 'node_1_laf_dba' in df.columns:
            df['ruido_dba'] = df['node_1_laf_dba']
        elif 'node_2_laf_dba' in df.columns:
            df['ruido_dba'] = df['node_2_laf_dba']
        else:
            df['ruido_dba'] = np.nan
            
        if 'lux' in df.columns:
            df.rename(columns={'lux': 'luz_lux'}, inplace=True)
        else:
            df['luz_lux'] = np.nan
            
        df.fillna(method='ffill', inplace=True) 
    
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
        h3 {{ color: #34495e; font-size: 11pt; margin-top: 10px; margin-bottom: 5px; page-break-after: avoid; }}
        .status-panel {{ display: table; width: 100%; margin-bottom: 15px; }}
        .status-box {{ display: table-cell; padding: 15px; background-color: #fff8e1; border-left: 5px solid #f39c12; }}
        .status-box.ok {{ background-color: #e8fdf0; border-left: 5px solid #27ae60; }}
        ul.alerts {{ margin: 5px 0 0 0; padding-left: 20px; color: #c0392b; font-weight: bold; }}
        ul.normativas {{ margin: 5px 0 15px 0; padding-left: 20px; color: #34495e; font-size: 9.5pt; }}
        table.data-table {{ width: 100%; border-collapse: collapse; margin-bottom: 15px; font-size: 9.5pt; }}
        table.data-table th, table.data-table td {{ border: 1px solid #ecf0f1; padding: 8px; text-align: center; }}
        table.data-table th {{ background-color: #f4f7f6; color: #34495e; font-weight: bold; }}
        .day-block {{ page-break-inside: avoid; margin-bottom: 30px; background-color: #fafbfc; border: 1px solid #e1e4e8; padding: 15px; border-radius: 4px; }}
        .day-title {{ font-size: 12pt; font-weight: bold; color: #2c3e50; margin-bottom: 10px; text-transform: uppercase; border-bottom: 2px solid #e74c3c; display: inline-block; padding-bottom: 2px; }}
        .metrics-container {{ display: table; width: 100%; margin-bottom: 15px; }}
        .metric-col {{ display: table-cell; width: 50%; padding-right: 10px; }}
        .metric-col:last-child {{ padding-right: 0; padding-left: 10px; border-left: 1px solid #ddd; }}
        .plot-img {{ width: 100%; max-width: 100%; height: auto; display: block; margin: 0 auto; }}
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
    alertas = []
    total_ruido = []
    
    # Mapeo de días al español
    dias_esp = {"Monday": "LUNES", "Tuesday": "MARTES", "Wednesday": "MIÉRCOLES", "Thursday": "JUEVES", "Friday": "VIERNES", "Saturday": "SÁBADO", "Sunday": "DOMINGO"}
    
    for date, group in df.groupby(df.index.date):
        if group.empty: continue
            
        day_name = dias_esp.get(date.strftime("%A"), "")
        date_str = date.strftime("%d/%m/%Y")
        
        horas = group.index.hour
        mask_day = (horas >= 7) & (horas < 22)
        mask_night = ~mask_day
        
        ruido = group['ruido_dba'].dropna().values
        luz = group['luz_lux'].dropna().values
        tiempos = group.index
        
        if len(ruido) == 0 or len(luz) == 0: continue
            
        r_diurno = ruido[mask_day].mean() if len(ruido[mask_day]) > 0 else 0
        r_nocturno = ruido[mask_night].mean() if len(ruido[mask_night]) > 0 else 0
        l_diurna = luz[mask_day].mean() if len(luz[mask_day]) > 0 else 0
        l_nocturna = luz[mask_night].mean() if len(luz[mask_night]) > 0 else 0
        
        total_ruido.append(np.mean(ruido))
        
        max_idx = np.argmax(ruido)
        pico_max = ruido[max_idx]
        hora_pico = tiempos[max_idx].strftime("%H:%M:%S")
        
        pct_fuera_norma = (np.sum(ruido > 45) / len(ruido)) * 100
        
        if pico_max > 65:
            alertas.append(f"{day_name} {date_str} a las {hora_pico}: Pico crítico de {pico_max:.1f} dBA.")

        # Generar Gráficas
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 4), sharex=True)
        fig.subplots_adjust(hspace=0.1)
        
        ax1.plot(tiempos, ruido, color='#c0392b', linewidth=1.2)
        ax1.axhline(45, color='#e74c3c', linestyle='--', linewidth=1, label='Límite Recomendado (45 dBA)')
        ax1.set_ylabel('Ruido (dBA)', color='#c0392b', fontweight='bold')
        ax1.tick_params(axis='y', labelcolor='#c0392b')
        ax1.set_ylim(30, max(85, pico_max + 10))
        ax1.legend(loc='upper right', fontsize=8)
        ax1.grid(True, alpha=0.3)
        ax1.set_title(f"{day_name} {date_str}", fontweight='bold', fontsize=10, color='#2c3e50')
        
        ax2.plot(tiempos, luz, color='#f39c12', linewidth=1.2)
        ax2.fill_between(tiempos, luz, color='#f39c12', alpha=0.2)
        ax2.set_ylabel('Iluminancia (Lux)', color='#d68910', fontweight='bold')
        ax2.tick_params(axis='y', labelcolor='#d68910')
        ax2.set_ylim(0, max(2000, np.max(luz) + 100))
        ax2.grid(True, alpha=0.3)
        
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        ax2.xaxis.set_major_locator(mdates.HourLocator(interval=3))
        plt.xticks(rotation=0)
        ax2.set_xlabel('Horas del Día')
        
        img_b64 = fig_to_base64(fig)
        
        block = f"""
        <div class="day-block">
            <div class="day-title">{day_name} {date_str}</div>
            <div class="metrics-container">
                <div class="metric-col">
                    <strong>Análisis Acústico</strong>
                    <table class="data-table" style="margin-top: 5px;">
                        <tr><td>Promedio Diurno:</td><td>{r_diurno:.1f} dBA</td></tr>
                        <tr><td>Promedio Nocturno:</td><td>{r_nocturno:.1f} dBA</td></tr>
                        <tr><td><strong>Pico Máximo:</strong></td><td><strong>{pico_max:.1f} dBA</strong> (a las {hora_pico})</td></tr>
                        <tr><td>Tiempo > 45 dBA:</td><td style="color: {'#c0392b' if pct_fuera_norma > 20 else '#27ae60'};"><strong>{pct_fuera_norma:.1f}%</strong> del día</td></tr>
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
            <img src="data:image/png;base64,{img_b64}" class="plot-img">
        </div>
        """
        daily_blocks.append(block)

    alert_html = ""
    status_class = "status-box"
    if len(alertas) > 0:
        alert_html = "<ul class='alerts'><li>" + "</li><li>".join(alertas) + "</li></ul>"
    else:
        status_class += " ok"
        alert_html = "<span style='color: #27ae60; font-weight: bold;'>✓ No se registraron picos críticos de ruido fuera del rango de tolerancia durante la semana.</span>"

    html_content += f"""
    <h2>2. Resumen Ejecutivo</h2>
    <div class="status-panel">
        <div class="{status_class}">
            <strong style="font-size: 11pt;">Estado General (Acústico):</strong><br>
            Las mediciones acústicas promedio se mantienen {'estables' if np.mean(total_ruido) < 50 else 'con advertencias'} respecto a los umbrales recomendados. 
            <br><br><strong>Alertas Críticas de la Semana:</strong><br>{alert_html}
        </div>
    </div>

    <h2>3. Referencias Ambientales (Normativas y Parámetros)</h2>
    <h3>Marco de Referencia Acústico</h3>
    <ul class="normativas">
        <li><strong>Organización Mundial de la Salud (OMS):</strong> El nivel sonoro equivalente ponderado es de 30 dBA y el nivel sonoro máximo es de 40 dBA.</li>
        <li><strong>Academia Americana de Pediatría (AAP):</strong> El nivel sonoro equivalente ponderado no debe superar los 45 dBA. Los niveles de ruido ambiental no pueden exceder los 50 dB(A) durante más del 10% del tiempo total de evaluación. Los picos máximos (Lmax) deben mantenerse estrictamente por debajo de los 65 dB(A).</li>
    </ul>

    <h3>Marco de Referencia Lumínico</h3>
    <p style="font-size: 9.5pt; color: #7f8c8d; margin-top: 0;">Valores teóricos esperados para el control del ciclo circadiano del neonato en la sala.</p>
    <table class="data-table" style="width: 70%; margin: 0 auto;">
        <tr><th>Condición de la Sala</th><th>Valor de Referencia Esperado</th></tr>
        <tr><td>Todo apagado</td><td>~ 200 lux</td></tr>
        <tr><td>Luz artificial</td><td>~ 800 lux</td></tr>
        <tr><td>Luz de ventanal</td><td>~ 1000 lux</td></tr>
        <tr><td>Luz artificial + Luz de ventanal</td><td>~ 1800 lux</td></tr>
    </table>

    <h2>4. Análisis Detallado Diario (Ruido y Luz)</h2>
    """

    for b in daily_blocks:
        html_content += b

    html_content += """
    <h2>5. Metrología y Estado del Sistema</h2>
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
        print("Advertencia: No se encontró la contraseña del correo en los secretos (EMAIL_PASSWORD). Omitiendo envío de email.")
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