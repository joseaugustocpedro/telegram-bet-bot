# Telegram Bet Bot — v3 Lite

Gestor de banca com dashboard web, liquidação por botões, depósitos/saques, alertas de risco e exportação CSV. Foi desenhado para usar pouca RAM:

- sem Matplotlib, Pandas, NumPy, Redis ou Celery;
- gráficos desenhados no navegador com SVG;
- consultas agregadas no PostgreSQL;
- exportação por cursor em lotes;
- compatível com a tabela antiga e cria as novas colunas automaticamente.

## Execução isolada

```bash
pip install -r requirements.txt
python bot.py
```

No serviço combinado, este projeto é importado pelo `main.py` e recebe updates por webhook.
