# Resiliencia de la Infraestructura de Internet en Chile (Terremoto 27F - Mw 8.8)

## Ejecución rápida

1. **Levantar la base de datos PostGIS + pgRouting 3.8:**
   ```bash
   docker compose up -d
   ```

2. **Instalar dependencias de Python**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Ejecutar el pipeline completo y levantar el visualizador:**
   ```bash
   python main.py
   ```
  