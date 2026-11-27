# Pruebas automáticas

Comprueban las piezas de KaBor que más importan, sin conectarse a MyFundedPerps:

| Archivo | Qué prueba |
|---|---|
| `test_motor.py` | Que todas las estrategias cargan y sus valores por defecto son válidos. El backtest es reproducible y nunca hay dos operaciones a la vez. Con varias estrategias, cada operación tiene su dueña. La dirección de cada estrategia se respeta. Una estrategia de 30m en un bot de 5m da las mismas operaciones que en un bot de 30m. |
| `test_lecturas.py` | El límite de lecturas por clave, la prioridad de lo de operar, el 429 con `Retry-After`, el aviso `X-RateLimit-Warning` y que solo se acepta el sandbox. |
| `test_stream_cuenta.py` | El stream de la cuenta contra un sandbox simulado: entrada, venta parcial y cierre sin lecturas REST; corte, lectura por la API y reanudación. Necesita `websockets` (`pip install websockets`); sin él, se salta. |
| `test_mcp.py` | El conector SMT: protocolo, herramientas con sus anotaciones (solo lectura, destructiva…) y token. |
| `test_traducciones.py` | Los diccionarios de inglés y francés: JSON válido, sin textos vacíos, con los mismos huecos `{0}` y las mismas claves. |

Para ejecutarlas:

```
./bot.sh pruebas
```

o, desde la carpeta de KaBor, `python3 -m unittest discover -s tests -v`. Tardan unos segundos y solo usan la biblioteca
estándar de Python (salvo el sandbox simulado). Las velas son sintéticas: siempre las mismas, sin conexión.
