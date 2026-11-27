# KaBor Quant 9.4.4 — papel y cuentas sandbox de MyFundedPerps

Un solo programa ejecuta varios bots a la vez con datos de MyFundedPerps en tiempo real y un panel web central.
Cada bot funciona **en papel** (como hasta la 4.0) o, si lo activas, **también en una o varias cuentas sandbox**:
las mismas señales, el mismo stop y el mismo objetivo se convierten en órdenes reales en cada cuenta.

## Numeración de las versiones

Desde la 9.2.1: **X.Y** para cambios importantes (funciones nuevas, cambios en el núcleo) y **X.Y.Z** para
cambios menores (pantallas, arreglos, textos).

## Qué hay de nuevo en la 9.4.4: planes y cambio de nombre del símbolo

* **Arreglo.** Si cambias el símbolo de un bot «Plan manual» (por ejemplo, de NAS100 a XYZ100, que es el mismo
  Nasdaq en MFP), sus planes viejos dejaban de encontrar su operación: en Planes y en SMT salían «disparados» pero sin
  R ni motivo. Ahora el símbolo se compara por instrumento (los mismos alias que usa KaBor para buscar el mercado),
  así que vuelven a mostrar su resultado. Lo mismo vale para:
  * los planes activos guardados con el nombre anterior: el bot los sigue vigilando;
  * la vista previa del tamaño y el riesgo de un plan;
  * `kabor_dibujos` de SMT: si pides NAS100 y los dibujos están en XYZ100, te da los de XYZ100.
* Si aun así no hay coincidencia, vale la operación del único bot «Plan manual» de esa temporalidad que entró en
  la misma vela del plan.
* 5 pruebas nuevas en `tests/test_planes.py` (27 en total).

Se instala igual que la 9.4, con `kabor_9.4.4_raspberry.zip`.

## Qué hay de nuevo en la 9.4.3: anotaciones de SMT y pruebas automáticas

* **Anotaciones MCP.** Cada herramienta de SMT dice ahora qué es: título, si solo lee (`readOnlyHint`), si puede
  borrar algo (`destructiveHint`), si repetirla da lo mismo (`idempotentHint`) y si sale de KaBor (`openWorldHint`).
  * Solo leen: `kabor_contexto`, `kabor_velas`, `kabor_dibujos`, `kabor_planes`, `kabor_bots`,
    `kabor_operaciones` y `kabor_estadisticas`.
  * `kabor_crear_plan` crea un plan pendiente sin operar.
  * `kabor_cancelar_pendiente` cancela uno.

  Claude lo tiene en cuenta (por ejemplo, para pedir confirmación antes de escribir). Para verlo, desconecta y
  vuelve a conectar el conector en Claude.
* **Pruebas automáticas** en la carpeta `tests/`: `./bot.sh pruebas`. Comprueban, sin conectarse a MFP:
  * el motor y el backtest;
  * varias estrategias por bot (una operación a la vez, dueña de cada operación, dirección y temporalidad propias);
  * el límite de lecturas, el 429 y el aviso de MFP;
  * el stream de la cuenta contra un sandbox simulado;
  * el conector MCP y los diccionarios de traducción.

  Son 22 pruebas que tardan unos segundos. Las del stream necesitan `pip install websockets`; sin él, se saltan.
* Ambas cosas las pedía el índice de confianza de MCP de M8ven, que analizó el repositorio público.

Se instaló igual que la 9.4, con `kabor_9.4.3_raspberry.zip`.

## Qué hay de nuevo en la 9.4.2: repaso del panel y de las traducciones

He revisado todas las páginas del panel en español, inglés y francés, en ordenador y en móvil, con los temas Cristal
(claro y oscuro) y Clásico.

**Traducciones**
* Las estrategias ya salen en inglés y francés: sus ajustes, sus descripciones y lo que explican en el panel del bot
  («Esperando un cierre fuera del canal…»). Son 286 textos de las estrategias incluidas.
* Los nombres de los bots con varias estrategias («Cruce de EMAs + RSI por niveles») se traducen en todas partes.
* También se traducen:
  * el tipo de cada evento en la actividad del bot y la lista de ajustes cambiados;
  * el estado de las cuentas;
  * una línea del Vigilante que mezclaba idiomas;
  * algunos avisos y etiquetas que faltaban.
* El registro del sistema (Ajustes) y los avisos de Telegram siguen en español.

**Pantalla**
* En el gráfico de precio del bot, las etiquetas del eje ya no se cortan con precios largos (BTC).
* Las tablas de la Ayuda ya no se salen de la pantalla.
* En el móvil, el selector «Partir de los valores por defecto» de Bot nuevo ya no se sale de la pantalla.
* Los campos del gráfico de análisis (Planes) y los selectores de Análisis tienen el mismo estilo que el resto.
* Con el envío de órdenes apagado, las cuentas muestran «SIN ENVÍO DE ÓRDENES» en vez de «SIN CONEXIÓN».
* Corregidas dos faltas: «ampliaciónes» y «operaciónes».

Se instala igual que la 9.4, con `kabor_9.4.2_raspberry.zip`.

## Qué hay de nuevo en la 9.4.1

* Arreglado: en Estadísticas, la barra de filtros (bot, largos/cortos, R/$) se montaba encima de las tarjetas.
  Unos estilos del informe «Rendimiento por estrategia» de la 9.1 usaban los mismos nombres que esa barra y la
  leyenda de la comparación. Ahora tienen nombres propios.
* Se instala igual que la 9.4, con el zip `kabor_9.4.1_raspberry.zip`.

## Qué hay de nuevo en la 9.4: stream de la cuenta y límite de lecturas de MyFundedPerps

MyFundedPerps va a limitar las lecturas (GET a `/v1/*`) a **30 por minuto por clave en sandbox**. Hay 7 días de
aviso (respuestas con `X-RateLimit-Warning`) y después las devolverá con un 429. Enviar, cambiar y cerrar órdenes
no cuenta. KaBor gastaba poco en reposo, pero cada apertura o cierre real hacía 8–14 lecturas en pocos segundos.
Con dos bots actuando a la vez en la misma cuenta se pasaba del límite.

* **Stream de la cuenta** (`/v1/sandbox/stream`), uno por clave. MFP envía al momento cada cambio de órdenes,
  llenados, posiciones y saldo. KaBor ya no consulta la posición en cada vela, ni la orden hasta que se llena, ni
  las órdenes de salida.
  * Una entrada pasa de ~12 lecturas a 1–3: la trading policy se guarda 10 minutos y el precio sale del flujo de
    mercado.
  * Un cierre o una venta parcial, a ninguna.
  * Si el stream se corta, se reconecta solo (reanudando donde iba) y, mientras tanto, se usa la API como antes.
* **Límite propio por clave:** como mucho 24 lecturas por minuto. La lectura periódica del panel se queda en 14
  para dejar sitio a lo de operar. Si MFP frena con un 429, KaBor espera el `Retry-After` completo (60 s) antes
  de volver a leer.
* **Avisos:** cada `X-RateLimit-Warning` o 429 de MFP queda en el registro, y llega un aviso de «problemas» por
  Telegram (como mucho uno por hora y clave).
* **En Cuentas**, cada cuenta muestra el estado del stream y las lecturas por minuto: ahora, máximo de la última
  hora y de 24 h.
* Las cuentas en papel no cambian. Los bots, las estrategias y los ajustes, tampoco.

### Instalar la 9.4

Deja el zip en tu carpeta personal (`~`), no dentro de `~/kcbot2`. Primero, la copia de seguridad:

```
cd ~/kcbot2 && ./bot.sh parar
cd ~ && tar czf kcbot2_copia_$(date +%Y%m%d_%H%M).tgz --exclude=venv kcbot2
unzip -o ~/kabor_9.4_raspberry.zip -d ~/kcbot2
cd ~/kcbot2 && ./bot.sh arrancar
```

Recarga el panel con Ctrl+F5 y mira en **Cuentas** que «Stream de la cuenta» diga «conectado». Para volver atrás:

```
cd ~/kcbot2 && ./bot.sh parar
cd ~ && mv kcbot2 kcbot2_descartada
tar xzf kcbot2_copia_AAAAMMDD_HHMM.tgz
mv kcbot2_descartada/venv kcbot2/
cd ~/kcbot2 && ./bot.sh arrancar
```

## Qué hay de nuevo en la 9.3: dirección por estrategia, desglose detallado y análisis desde el portátil

* **Dirección de cada estrategia.** «Dirección» (ambos, solo largos, solo cortos) ya no es del bot: cada estrategia
  tiene la suya en su pestaña, en el grupo «Temporalidad y dirección», también la principal. Si una funciona mejor
  solo en ventas, la dejas solo en cortos sin tocar a las demás. Vale en directo, en el backtest y en la puntuación.
  Las configuraciones guardadas conservan lo que tenían: la dirección que era del bot pasa a todas sus estrategias.
* **Desglose detallado.** Al probar en el histórico, el botón «Desglose detallado» abre una ventana flotante con:
  * la comparación de las estrategias y la curva de cada una;
  * la línea de tiempo de cada estrategia, con cada señal (▲/▼) y lo que pasó con ella: entra, bloqueada por la
    operación de otra, filtrada o límite del día;
  * quién bloquea a quién;
  * una ficha por estrategia con largos frente a cortos, horas, días, motivos de salida e ideas para probar.

  «Descargar datos» guarda la prueba en JSON.
* **`herramientas/analizar_estrategias.py`, para el portátil.** Pide a la Raspberry muchas pruebas del mismo bot y
  escribe un informe HTML con CSV para Excel. Prueba:
  * cada estrategia sola;
  * el bot sin cada una;
  * cada dirección de cada estrategia;
  * cada orden de prioridad.

  El informe incluye las señales que cada estrategia bloqueó a otra y lo que habrían dado, las señales contrarias a
  la operación abierta, la línea de tiempo y las señales por hora. Solo necesita Python 3, sin instalar nada:

  ```
  python analizar_estrategias.py --pi https://DIRECCIÓN-DEL-PANEL --usuario TU_USUARIO --bot 3 --dias 90
  ```

  La contraseña (y el código de dos pasos) se piden al ejecutarlo y no se guardan. Con el certificado propio de la
  Raspberry: `--certificado cert.pem` (copia `data/tls/cert.pem`), Tailscale o, solo en casa, `--sin-verificar`.
  `--rapido` se salta los órdenes de prioridad; `--archivo desglose_….json` analiza lo descargado del panel sin
  conectar. Las pruebas se hacen en la Raspberry y los bots siguen funcionando.
* La Ayuda (español, inglés y francés) explica todo esto con capturas.

### Instalar la 9.3

Deja el zip en tu carpeta personal (`~`), no dentro de `~/kcbot2`. Primero, la copia de seguridad:

```
cd ~/kcbot2 && ./bot.sh parar
cd ~ && tar czf kcbot2_copia_$(date +%Y%m%d_%H%M).tgz --exclude=venv kcbot2
unzip -o ~/kabor_9.3_raspberry.zip -d ~/kcbot2
cd ~/kcbot2 && ./bot.sh arrancar
```

Recarga el panel con Ctrl+F5. Para volver atrás:

```
cd ~/kcbot2 && ./bot.sh parar
cd ~ && mv kcbot2 kcbot2_descartada
tar xzf kcbot2_copia_AAAAMMDD_HHMM.tgz
mv kcbot2_descartada/venv kcbot2/
cd ~/kcbot2 && ./bot.sh arrancar
```

Para el portátil, copia `~/kcbot2/herramientas/analizar_estrategias.py` (por ejemplo con
`scp usuario@raspberry:kcbot2/herramientas/analizar_estrategias.py .`).

## Qué hay de nuevo en la 9.2.2: la Ayuda en tu idioma

* La guía del menú **Ayuda** está en español, inglés y francés, y se abre en el idioma del panel (Ajustes → Idioma). Si cambias de idioma, la guía cambia con él.
* Las capturas de la guía coinciden con el idioma: cada idioma tiene las suyas (`web/ayuda/img/en/`, `web/ayuda/img/fr/`), también el esquema del sistema.
* La referencia de ajustes y la lista de estrategias de la guía salen traducidas.
* Los avisos de Telegram siguen en español.

### Instalar la 9.2.2

Deja el zip en tu carpeta personal (`~`), no dentro de `~/kcbot2`. Primero, la copia de seguridad:

```
cd ~/kcbot2 && ./bot.sh parar
cd ~ && tar czf kcbot2_copia_$(date +%Y%m%d_%H%M).tgz --exclude=venv kcbot2
unzip -o ~/kabor_9.2.2_raspberry.zip -d ~/kcbot2
cd ~/kcbot2 && ./bot.sh arrancar
```

Recarga el panel con Ctrl+F5. Para volver atrás:

```
cd ~/kcbot2 && ./bot.sh parar
cd ~ && mv kcbot2 kcbot2_descartada
tar xzf kcbot2_copia_AAAAMMDD_HHMM.tgz
mv kcbot2_descartada/venv kcbot2/
cd ~/kcbot2 && ./bot.sh arrancar
```

## Qué hay de nuevo en la 9.2.1: configuración en pestañas y menú Ayuda

- **Configuración en pestañas.** Dentro de la configuración del bot, la pestaña **Bot** tiene lo que es del bot
  (mercado, riesgo, límites, filtros, ejecución) y hay una pestaña por cada estrategia con sus ajustes propios y su
  temporalidad. El número de cada pestaña son sus cambios sin guardar.
- **Menú Ayuda** (Sistema → Ayuda): la guía completa del sistema con capturas, índice, buscador y opción de imprimir
  o guardar en PDF. La referencia de ajustes y la lista de estrategias salen del propio programa, así que siempre
  están al día. Los archivos están en `web/ayuda/`.

### Actualizar a la 9.2.1 con copia de seguridad

Incluye todo lo de la 9.2, que cambia el núcleo, así que antes haz una copia de todo. Deja `kabor_9.2.1_raspberry.zip` en tu carpeta personal (`~`),
no dentro de `~/kcbot2`: se descomprime directamente en el programa.

**1. Copia de seguridad** (con el programa parado):

```
cd ~/kcbot2 && ./bot.sh parar
cd ~ && tar czf kcbot2_copia_antes_9.2.1_$(date +%Y%m%d_%H%M).tgz --exclude=venv kcbot2
ls -lh ~/kcbot2_copia_antes_9.2.1_*.tgz
```

**2. Instalar y arrancar:**

```
unzip -o ~/kabor_9.2.1_raspberry.zip -d ~/kcbot2
cd ~/kcbot2 && ./bot.sh arrancar
./bot.sh logs        # Ctrl+C para salir; en Telegram debe llegar «Programa en marcha (v9.2.1)»
```

Recarga el panel con Ctrl+F5.

**3. Volver a la versión anterior** (solo si algo va mal):

```
cd ~/kcbot2 && ./bot.sh parar
cd ~ && mv kcbot2 kcbot2_9.2.1_descartada
tar xzf kcbot2_copia_antes_9.2.1_AAAAMMDD_HHMM.tgz      # el nombre de tu copia (ls ~/kcbot2_copia_*)
mv kcbot2_9.2.1_descartada/venv kcbot2/
cd ~/kcbot2 && ./bot.sh arrancar
```

## Qué hay de nuevo en la 9.2: cada estrategia con su temporalidad y sus ajustes

- **Temporalidad propia por estrategia.** En un bot con varias estrategias, cada una elige sus velas: «Temporalidad
  de la estrategia principal» (Mercado) y «Temporalidad» en el grupo de cada estrategia añadida. Tiene que ser igual
  o mayor que la del bot y múltiplo de ella, así que **pon el bot en la temporalidad más pequeña** (p. ej. bot de 5m
  con un ORB en 5m, una Ruptura de Keltner en 30m y un Supertrend en 4h). El bot construye las velas de cada
  estrategia con las suyas (las de 4h y 1d, en horas UTC como Binance y TradingView); cada estrategia ve solo sus velas
  cerradas, con su ATR, su vela anterior y su contador, así que las estrategias no hay que tocarlas.
- Las señales entran en la apertura de la siguiente vela del bot (una señal de 4h no espera 4 horas). El stop, el
  objetivo, el break-even y el trailing se vigilan en cada vela del bot; la salida propia de una estrategia (p. ej.
  cruce contrario) se mira al cierre de sus velas. Sigue habiendo una sola operación a la vez.
- **Ajustes propios aunque se llamen igual.** Los de cada estrategia añadida llevan su id delante por dentro
  (`supertrend__ema_on`), así que se pueden combinar estrategias con ajustes del mismo nombre y cada una conserva
  sus valores. Lo guardado con la 9.1 se conserva.
- La puntuación prueba cada estrategia en su temporalidad; el informe por estrategia, el desglose del backtest y los
  avisos de Telegram dicen la temporalidad de cada una («🧩 Ruptura de Keltner (30m)»).
- Si MyFundedPerps cambia un mercado de sitio en su catálogo (el Nasdaq pasó a `composite|XYZ100`), KaBor lo busca
  por su nombre, se actualiza solo y lo avisa.

## Qué hay de nuevo en la 9.1: varias estrategias en un bot

### Actualizar a la 9.1 con copia de seguridad

Cambia el núcleo (`nucleo.py`) y el formato de algunos datos (columna `estrategia` en `operaciones.csv`,
puntuaciones en `data/bots.json`), así que antes de instalar haz una copia de todo. Deja `kabor_9.1_raspberry.zip`
en tu carpeta personal (`~`), no dentro de `~/kcbot2`: se descomprime directamente en el programa.

**1. Copia de seguridad** (con el programa parado, para que los datos queden coherentes):

```
cd ~/kcbot2 && ./bot.sh parar
cd ~ && tar czf kcbot2_copia_antes_9.1_$(date +%Y%m%d_%H%M).tgz --exclude=venv kcbot2
ls -lh ~/kcbot2_copia_antes_9.1_*.tgz      # comprueba que existe y que no pesa 0
```

Mientras está parado no entran operaciones nuevas. Las posiciones abiertas en las cuentas sandbox siguen con su
stop y su objetivo en MyFundedPerps.

**2. Instalar y arrancar:**

```
unzip -o ~/kabor_9.1_raspberry.zip -d ~/kcbot2
cd ~/kcbot2 && ./bot.sh arrancar
./bot.sh logs        # Ctrl+C para salir; en Telegram debe llegar «Programa en marcha (v9.1)»
```

Recarga el panel con Ctrl+F5.

**3. Volver a la versión anterior** (solo si algo va mal). Restaura el programa y los datos tal como estaban al
hacer la copia; lo que haya pasado después con la 9.1 queda guardado en `~/kcbot2_9.1_descartada`:

```
cd ~/kcbot2 && ./bot.sh parar
cd ~ && mv kcbot2 kcbot2_9.1_descartada
tar xzf kcbot2_copia_antes_9.1_AAAAMMDD_HHMM.tgz      # el nombre de tu copia (ls ~/kcbot2_copia_*)
mv kcbot2_9.1_descartada/venv kcbot2/                  # la copia no incluye el entorno de Python
cd ~/kcbot2 && ./bot.sh arrancar
```

- **Varias estrategias en un bot** (Configuración → Mercado → «Más estrategias en este bot», hasta 4 más). El bot
  sigue en un solo instrumento y con **una operación a la vez**: la primera señal válida abre y las demás quedan
  bloqueadas hasta que esa operación se cierra (TP, stop, salida de la estrategia que la abrió, o a mano); las
  señales contrarias no cierran ni abren nada. Cada estrategia gestiona la salida de sus operaciones con sus ajustes,
  que salen en el formulario en un grupo con su nombre. El riesgo, los límites, el horario y los filtros son del bot.
- **Prioridad automática por puntuación**: si varias dan señal en la misma vela, manda la de mejor puntuación. La
  puntuación es el SQN (media de R / desviación × √operaciones, hasta 100) del backtest de cada estrategia sola en
  los últimos 90 días con los ajustes del bot; menos de 10 operaciones = sin puntuación (va la última). Se calcula
  sola al guardar la configuración y cada día, o con «Recalcular puntuación». Se guarda en `data/bots.json`.
- **Rendimiento por estrategia** (Panel del bot): barras de R en directo y en el backtest, curva de cada estrategia,
  y una ficha por estrategia con su prioridad, puntuación, operaciones, acierto, PF, drawdown, $ y señales bloqueadas.
  El backtest de un bot combinado también da el desglose por estrategia. Los avisos de Telegram dicen qué estrategia
  dio la señal. `operaciones.csv` tiene una columna nueva, `estrategia` (las antiguas se completan solas).
- El «Plan manual» de SMT sigue en su propio bot: no se combina.

## Qué hay de nuevo en la 9.0: análisis dibujado en el gráfico y TradingView

- **Gráfico de análisis** (Planes): velas reales de MFP con herramientas de dibujo. **Nivel** (entrada, stop, TP1,
  TP final, invalidación o nivel libre), **Zona** (FVG alcista o bajista, order block, liquidez o zona libre) y
  **Tendencia**. Se dibuja con el ratón o con el dedo (arrastrando o tocando dos puntos), se mueve arrastrando y
  cada objeto se edita en la lista de debajo (precios exactos, nota, «hasta ahora»). Se guarda en `data/dibujos.json`.
- **▲ Largo / ▼ Corto** (como la herramienta de posición de TradingView): tocas la entrada y aparece la caja verde
  (beneficio) y roja (riesgo). Arrastras la entrada, el stop, el TP y el TP1, o la caja entera; el borde derecho
  cambia el ancho. Las etiquetas dicen precio, %, $ y R:R, con los contratos calculados con el riesgo del bot «Plan
  manual» (o con el riesgo o los contratos que escribas en la lista). «Crear plan» rellena el formulario con ella.
- **Marcar FVG**: busca los huecos de tres velas que siguen abiertos y marca los 6 más cercanos al precio (línea
  discontinua). El mismo botón los quita.
- **Crear plan desde el gráfico**: rellena «Plan a mano» con tus niveles (lado, orden límite en la entrada, stop,
  TP1, TP e invalidación) y un resumen de tus zonas. Lo revisas y lo creas como pendiente.
- Los planes pendientes y activos del instrumento se ven en el gráfico en línea discontinua.
- **SMT**: herramienta nueva `kabor_dibujos`. Claude lee tus niveles y zonas, con el estado de cada zona (sin tocar,
  tocada, rellenada) y el R:R de tus niveles, y prepara el plan a partir de ellos. Sigue sin poder confirmarlo.
- **Análisis** (menú): gráfico de TradingView del mismo mercado que usa MFP (`BINANCE:XXXUSDT.P`,
  `HIP3XYZ:XYZ100USDC.P`), con el símbolo elegido solo y editable en la misma página.
- Gráficos con TradingView Lightweight Charts (Apache 2.0, incluida en `web/vendor/`).

## Qué hay de nuevo en la 8.9: temporalidades 2h, 4h, diaria y semanal

Un bot ya puede ir en **1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 1d y 1w**. También los planes manuales (y el conector de
Claude: `kabor_velas` y `kabor_crear_plan` aceptan 2h, 4h, 1d y 1w) y el filtro de tendencia, que suma **1w**.

- **2h y 4h** vienen tal cual de MFP.
- **1d y 1w** los construye KaBor con velas de 4h: **días UTC** (de 00:00 a 00:00 UTC, 01:00 en España en invierno)
  y **semanas de lunes a domingo**, como en Binance y TradingView. Es igual con cualquier proveedor. Una vela diaria
  no se da por cerrada hasta que llega su última vela de 4h (o 15 min después del cierre si ese tramo no tuvo
  operaciones), así que el bot decide entre 1 y 2 minutos después del cierre.
- **Historial**: un bot diario descarga unas 400 velas y uno semanal unas 150; arranca con 120 y 40 como mínimo.
  Hyperliquid solo guarda unas 5.000 velas de 4h (2 años y medio): en diario sobra, pero en semanal da unas 120
  semanas. Un filtro de tendencia semanal con EMAs largas (por ejemplo EMA 200) no llega a calentar con esos datos:
  usa EMAs cortas o el diario.
- **Datos externos (CSV)**: al importar, además de 1m-4h se generan las series diaria y semanal, y se aceptan CSV
  diarios o semanales.
- **Estrategias**: todas funcionan en las nuevas temporalidades, pero las de sesión (ORB NY, ORB BTC) y las que miran
  dentro del día (IFVG) no dan señales en 4h o más, porque una vela ya contiene la sesión entera. Prueba siempre en
  el histórico antes de lanzar un bot en diario o semanal.
- En el panel, la hora de la última vela pasa a ser la fecha en diario y semanal.
- Arreglado: en Cristal la barra de «Probar / Lanzar» dejaba ver el texto de debajo; ahora es opaca. Y vuelve el
  borde rojo de los avisos y de la barra de bots detenidos, que se había perdido.

**Conector de Claude:** como cambian las temporalidades de las herramientas, desconecta y vuelve a conectar el
conector en Claude para que las vea.

```
cd ~ && tar czf kcbot2_copia_antes89.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v8_9.zip -d tmp89
cp tmp89/kcbot2/{kcbot.py,nucleo.py,planes.py,externos.py,temas.py,panel.html,LEEME.md} ~/kcbot2/
cp tmp89/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp89 && cd ~/kcbot2 && ./bot.sh comprobar && ./bot.sh reiniciar
```

Incluye todo desde la 8.7. Los bots que ya tienes no cambian.

## Qué hay de nuevo en la 8.8.3: scroll fluido en el móvil

El tema Cristal iba a tirones en el móvil al hacer scroll. Cada fotograma costaba unos 30 ms de tarjeta gráfica (el
móvil tiene 16 ms para que vaya fluido) por tres cosas: el fondo «fijo», que obliga a repintar toda la pantalla en
cada paso del scroll; el desenfoque de cristal en cada tarjeta y cada botón, metido dentro del desenfoque del panel
grande (no se nota, porque dentro solo hay un color casi liso, pero cuesta mucho); y la cinta de precios, que se mueve
sin parar y también tenía desenfoque. Ahora son unos 2,5 ms por fotograma, como Lima y Clásico, y se ve igual:

- El fondo va en una capa fija aparte (también el de los temas propios).
- Las tarjetas, la cinta y los botones no llevan desenfoque. En el móvil tampoco el panel grande: el fondo es un
  degradado suave y desenfocado se ve igual.
- Los gráficos ya no se redibujan cada vez que la barra del navegador aparece o desaparece al hacer scroll.
- Los efectos al pasar el ratón solo se activan donde hay ratón.

```
cd ~ && tar czf kcbot2_copia_antes883.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v8_8_3.zip -d tmp883
cp tmp883/kcbot2/{kcbot.py,temas.py,panel.html,LEEME.md} ~/kcbot2/
cp tmp883/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp883 && cd ~/kcbot2 && ./bot.sh comprobar && ./bot.sh reiniciar
```

Incluye todo desde la 8.7. Después, en el móvil cierra la app del panel y vuelve a abrirla.

## Qué hay de nuevo en la 8.8.2

- Arreglado: los botones «Panel / Configuración» de cada bot salían pegados a la izquierda, sin alinear con el resto
  en pantallas anchas. Pasaba lo mismo con la barra de bots detenidos y con la cinta de precios, que ahora ocupa todo
  el borde de arriba del panel (en Lima y con el menú arriba, alineada con el contenido).
- Arreglado: en el modo oscuro las listas desplegables (por ejemplo «Copiar ajustes de…») se abrían en blanco con
  letras casi blancas. Ahora se abren oscuras en el modo oscuro y claras en el claro, con cualquier tema.

```
cd ~ && tar czf kcbot2_copia_antes882.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v8_8_2.zip -d tmp882
cp tmp882/kcbot2/{kcbot.py,temas.py,panel.html,LEEME.md} ~/kcbot2/
cp tmp882/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp882 && cd ~/kcbot2 && ./bot.sh comprobar && ./bot.sh reiniciar
```

Incluye la 8.7, la 8.8 y la 8.8.1. Después, recarga el panel.

## Qué hay de nuevo en la 8.8.1

- Arreglado: en los temas Cristal y Lima el botón rojo de las ventanas de confirmación («Quitar», «Borrar», «Cerrar»...)
  salía sin texto (letras rojas sobre fondo rojo). Ahora es rojo con letras blancas en el modo claro y oscuras en el
  oscuro, en todos los temas.

```
cd ~ && tar czf kcbot2_copia_antes881.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v8_8_1.zip -d tmp881
cp tmp881/kcbot2/{kcbot.py,temas.py,panel.html,LEEME.md} ~/kcbot2/
cp tmp881/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp881 && cd ~/kcbot2 && ./bot.sh comprobar && ./bot.sh reiniciar
```

Incluye la 8.7 y la 8.8. Después, recarga el panel.

## Qué hay de nuevo en la 8.8: menú arriba

En el ordenador el menú puede ir **arriba**, como una barra de pastillas: Resumen, Estadísticas, Diario, Planes,
Retiros y gastos, Cartera y Vigilante a la vista, y **Bots** y **Herramientas** como desplegables (con los grupos de
bots, «Nuevo bot», las herramientas y Ajustes). La sección en la que estás se marca en negro; si hay un bot detenido o
un bloqueo, aparece un punto rojo en su desplegable. El contenido usa todo el ancho de la pantalla.

- Ajustes → Temas → **Menú en el ordenador**: «Según el tema» (arriba con Lima, lateral con los demás), «Lateral» o
  «Arriba». Funciona con cualquier tema, también con los propios. Se guarda en cada navegador.
- El desplegable se cierra al elegir una sección, al pulsar fuera o con Esc.
- En el móvil no cambia: sigue el menú de la esquina.

```
cd ~ && tar czf kcbot2_copia_antes88.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v8_8.zip -d tmp88
cp tmp88/kcbot2/{kcbot.py,temas.py,panel.html,LEEME.md} ~/kcbot2/
cp tmp88/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp88 && cd ~/kcbot2 && ./bot.sh comprobar && ./bot.sh reiniciar
```

Incluye todo lo de la 8.7 (si no la habías instalado, con esto basta). Después, recarga el panel.

## Qué hay de nuevo en la 8.7: temas («Lima» y temas propios desde un archivo)

**Ajustes → Temas** es una galería con una vista previa de cada aspecto. Pulsas uno y se aplica al momento.

- **Cristal** (el de la 8.6), **Lima** (nuevo: blanco minimalista, tarjetas blancas con sombra suave, píldoras negras
  para lo activo y acento lima en el botón principal y en el primer total del Resumen) y **Clásico**. Los tres tienen
  versión clara y oscura, que sigue la del sistema.
- La elección se guarda **en cada navegador**: puedes usar Lima en el ordenador y Cristal en el móvil.
- **Cargar tema (.json)**: sube un archivo de tema. Se guarda en la Raspberry (`data/temas/`), aparece en la galería de
  todos tus dispositivos y se puede borrar con la ✕ de su tarjeta. Máximo 30 temas de 20 KB.
- **Exportar el tema actual**: descarga un `.json` con los colores que estás viendo. Es la forma fácil de empezar uno:
  exportas, cambias colores y lo vuelves a cargar.

### Formato de un tema

```json
{
  "nombre": "Atardecer",
  "base": "cristal",
  "claro":  {"fondo": "linear-gradient(155deg,#7a2e4a,#f0a35e 60%,#8a3a3a)", "acento": "#b5432a", "texto": "#2a1414"},
  "oscuro": {"fondo": "#1a0c10", "acento": "#ff9a6a"},
  "todos":  {"esquinas": "22px"}
}
```

- `base`: `clasico`, `cristal` o `lima`. El tema parte de ese estilo y solo cambia lo que indiques.
- `claro` se aplica con el sistema en modo claro, `oscuro` en modo oscuro y `todos` en los dos. Todas las claves son
  opcionales.
- Claves: `fondo`, `contenedor`, `paneles`, `campos`, `texto`, `texto_suave`, `lineas`, `borde`, `sombra`, `acento`,
  `acento_suave`, `ganancia`, `perdida`, `aviso`, `azul`, `morado`, `destacado`, `destacado_texto`, `boton`,
  `boton_principal`, `boton_principal_texto`, `pastilla_activa`, `pastilla_activa_texto`, `esquinas`,
  `esquinas_pequenas`, `letra`, `menu_fondo`, `menu_fondo_2`, `menu_texto`, `menu_texto_suave`, `menu_activo`,
  `menu_acento`, `menu_hover`, `menu_lineas`.
- Valores: colores (`#0b7f96`, `rgba(...)`), degradados (`linear-gradient(...)`), medidas (`18px`) y nombres de letra.
  Por seguridad se rechaza cualquier otra cosa (`url()`, `@import`, código...): un tema no puede cargar nada de
  fuera ni ejecutar nada.

Los gráficos toman los colores del tema. La pantalla de inicio de sesión no cambia.

```
cd ~ && tar czf kcbot2_copia_antes87.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v8_7.zip -d tmp87
cp tmp87/kcbot2/{kcbot.py,temas.py,panel.html,LEEME.md} ~/kcbot2/
cp tmp87/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp87 && cd ~/kcbot2 && ./bot.sh comprobar && ./bot.sh reiniciar
```

Después, recarga el panel (en la app del móvil, ciérrala y vuelve a abrirla).

## Qué hay de nuevo en la 8.6: estilo «Cristal»

Nuevo aspecto del panel, inspirado en los paneles de administración *glassmorphism*: fondo degradado verde azulado,
paneles de cristal esmerilado translúcidos con esquinas muy redondeadas, menú lateral flotante, botones en forma de
píldora (el principal oscuro), acento cian y un ligero «levantamiento» de las tarjetas al pasar el ratón. Tiene
versión clara y oscura (sigue la del sistema).

- Viene activado. Para volver al de siempre: Ajustes → **Temas** → Clásico (desde la 8.7). Se guarda
  en cada navegador o móvil.
- El efecto de cristal lo dibuja el navegador del móvil u ordenador, no la Raspberry: no le añade carga. Si el sistema
  tiene activado «reducir transparencia», los paneles se vuelven opacos.
- Solo cambia el aspecto: misma información y mismas funciones. La pantalla de inicio de sesión no cambia.

```
cd ~ && unzip -o kabor_quant_v8_6.zip -d tmp86
cp tmp86/kcbot2/{kcbot.py,panel.html,LEEME.md} ~/kcbot2/
cp tmp86/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp86 && cd ~/kcbot2 && ./bot.sh reiniciar
```

Después, recarga el panel en el navegador (y, en la app del móvil, ciérrala y vuelve a abrirla).

## 8.5.1: lo asegurado con una venta parcial

Una venta parcial (del TP1 de un plan, del break-even del bot o manual) no crea una operación nueva: en las
estadísticas cuenta al cerrar la operación, como una sola con su resultado combinado. Para que se vea mientras está
abierta, la tarjeta de la operación muestra ahora **Asegurado** (los R ya cobrados con la venta parcial) y **Si salta
el stop** (los R con los que acabaría si toca el stop actual: con el stop en break-even tras vender una parte, es lo
asegurado).

```
cd ~ && unzip -o kabor_quant_v8_5_1.zip -d tmp851
cp tmp851/kcbot2/{kcbot.py,panel.html,LEEME.md} ~/kcbot2/
cp tmp851/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp851 && cd ~/kcbot2 && ./bot.sh reiniciar
```

## Qué hay de nuevo en la 8.5: gestión manual de la operación

En la página de cada bot, bajo la operación abierta, hay botones para gestionarla a mano, por si el mercado cambia o
el bot está mal configurado:

- **Stop a break-even**: el stop pasa a la entrada (solo si el precio va a favor).
- **Mover stop** y **Mover objetivo** a un precio (el objetivo se puede quitar dejándolo vacío). KaBor comprueba que
  queden en el lado correcto del precio actual. Un objetivo puesto a mano ya no lo cambian el objetivo dinámico de la
  estrategia ni el trailing.
- **Venta parcial** (5 a 90 %) a mercado, una por operación (si luego llega el TP1 de un plan, no vende otra parte).
- **Cerrar ahora** a mercado, con confirmación.

Se aplica al modelo del bot y, si el bot opera en cuenta, a la posición real al momento (stop y objetivo en la
cuenta, venta y cierre a mercado). Queda en el registro del bot y avisa por Telegram. El resultado de un cierre manual
aparece como «cierre manual» en las estadísticas.

Incluye todo desde la 7.7. Actualizar desde la 7.6 o posterior:

```
cd ~
tar czf kcbot2_copia_antes85.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v8_5.zip -d tmp85
cp tmp85/kcbot2/{kcbot.py,nucleo.py,planes.py,fiscal.py,ejecucion.py,panel.html,LEEME.md} ~/kcbot2/
cp tmp85/kcbot2/estrategias/plan_manual.py ~/kcbot2/estrategias/
cp tmp85/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp85
cd ~/kcbot2 && ./bot.sh comprobar
./bot.sh reiniciar
```

## 8.4.2: el bot muestra el plan en cuanto lo confirmas

El bot «Plan manual» leía los planes activos solo al cierre de cada vela. Tras confirmar un plan, el panel del bot
seguía diciendo «Sin planes activos» (y sin sus líneas en el gráfico) hasta el siguiente cierre. Ahora el texto y las
líneas se leen al momento. Para una orden límite indica que se colocará en el próximo cierre de vela.

```
cd ~ && unzip -o kabor_quant_v8_4_2.zip -d tmp842
cp tmp842/kcbot2/{kcbot.py,planes.py,LEEME.md} ~/kcbot2/
cp tmp842/kcbot2/estrategias/plan_manual.py ~/kcbot2/estrategias/
rm -r tmp842 && cd ~/kcbot2 && ./bot.sh reiniciar
```

## 8.4.1: corrección del aviso de órdenes límite en largos

El aviso «el precio actual ya está al otro lado del límite» salía al revés en los **largos**: avisaba con el precio
por encima de una buy limit, que es lo normal. Solo era el texto del aviso; el motor, la vigilancia en vivo y la
cuenta ya trataban bien los largos (la orden espera a que el precio baje hasta el límite). Ahora avisa solo cuando
de verdad se llenaría al colocarla: largo con el precio en el límite o por debajo, corto en él o por encima.

Solo cambian `planes.py` y `kcbot.py` (versión):

```
cd ~ && unzip -o kabor_quant_v8_4_1.zip -d tmp841
cp tmp841/kcbot2/{kcbot.py,planes.py,LEEME.md} ~/kcbot2/ && rm -r tmp841
cd ~/kcbot2 && ./bot.sh reiniciar
```

## Qué hay de nuevo en la 8.4: tamaño de la posición

Riesgo, tamaño y stop están atados: **riesgo ($) = contratos × distancia al stop × valor del punto** (en NAS100 de
MyFundedPerps, 1 $ por punto y unidad). Como el stop lo pone tu análisis, lo que eliges es **riesgo o tamaño**:

- **Planes:** nuevo campo **Tamaño** («Por el riesgo del bot», como hasta ahora, o «Contratos fijos»). Con contratos
  fijos, KaBor calcula lo que arriesgas. Ejemplo con el stop a 68 puntos: 175 $ de riesgo son unos 2,57 contratos;
  1 contrato son 68 $ de riesgo.
- **Vista previa** en cada plan vivo (panel y conector de Claude): por cada bot que lo vigila, el modo, los contratos,
  el riesgo en dólares y el nominal. Avisa si el nominal supera el máximo del bot (se reducirá) o si un tamaño fijo
  arriesga más de 1,5 veces lo habitual del bot.
- **Bots (también los algorítmicos):** en «Ejecución en sandbox → Tamaño de la posición» hay un tercer modo,
  **«tamaño fijo (contratos)»**, con su campo de contratos. Se siguen respetando el nominal máximo, el multiplicador
  de cada cuenta y las comprobaciones de la cuenta (daily loss, apalancamiento).
- En papel los resultados siguen en R, que es lo correcto para comparar estrategias y planes.

**Reconecta el conector de Claude** (desconectar y conectar, y chat nuevo): cambian sus herramientas.

Incluye todo desde la 7.7. Este paquete trae también `ejecucion.py`. Actualizar desde la 7.6 o posterior:

```
cd ~
tar czf kcbot2_copia_antes84.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v8_4.zip -d tmp84
cp tmp84/kcbot2/{kcbot.py,nucleo.py,planes.py,fiscal.py,ejecucion.py,panel.html,LEEME.md} ~/kcbot2/
cp tmp84/kcbot2/estrategias/plan_manual.py ~/kcbot2/estrategias/
cp tmp84/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp84
cd ~/kcbot2 && ./bot.sh comprobar
./bot.sh reiniciar
```

`./bot.sh comprobar` tiene que dar [ok] en todas las estrategias, con «Plan manual» v1.5.

## Qué hay de nuevo en la 8.3: TP1 con venta parcial y break-even en los planes

Un plan puede tener ahora **dos objetivos**: un **TP1** donde se cierra un porcentaje y, si quieres, el stop pasa a
**break-even**, y el **objetivo final (TP2)** para el resto.

- En el panel (Planes → «Plan a mano») y en el conector de Claude: TP1, porcentaje que se cierra (0 a 90 %; 0 = solo
  mover el stop) y «Stop a break-even al tocar el TP1».
- **En papel:** el motor vende la parte al tocar el TP1, sube el stop a la entrada y calcula el resultado combinado.
  Por ejemplo, largo en 27.718 con stop en 27.650, 20 % en 28.015 y resto en 28.356: si vuelve a break-even son unos
  +0,8 R; si llega al TP2, unos +8,3 R.
- **En la cuenta sandbox:** KaBor vigila el TP1 en vivo y, al tocarlo, cierra el porcentaje a mercado y mueve el stop
  a break-even sin esperar al cierre de la vela. El modelo del bot se actualiza igual para que cuadren.
- Si la venta parcial no se puede hacer en vivo (por ejemplo, la cuenta no responde), se hace al cierre de la vela,
  como antes con el break-even de los bots.
- La entrada al precio exacto ya existe desde la 8.2: la condición **«Orden límite en»**. En papel se llena en tu
  nivel; en la sandbox se vigila en vivo y entra a mercado al tocarlo.
- El motor gana un enganche (`gestion_be`) para que cualquier estrategia defina su propia venta parcial por operación.
  Las estrategias que no lo usan dan exactamente los mismos resultados.

**Importante:** el conector cambia sus herramientas. En Claude, Ajustes → Conectores → desconectar y volver a
conectar, y abrir un chat nuevo. Si no, Claude seguirá con la descripción antigua.

Incluye todo desde la 7.7. Actualizar a la 8.3 desde la 7.6 o posterior (si instalas la 8.4, usa sus comandos):

```
cd ~
tar czf kcbot2_copia_antes83.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v8_3.zip -d tmp83
cp tmp83/kcbot2/{kcbot.py,nucleo.py,planes.py,fiscal.py,panel.html,LEEME.md} ~/kcbot2/
cp tmp83/kcbot2/estrategias/plan_manual.py ~/kcbot2/estrategias/
cp tmp83/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp83
cd ~/kcbot2 && ./bot.sh comprobar
./bot.sh reiniciar
```

`./bot.sh comprobar` tiene que dar [ok] en todas las estrategias, con «Plan manual» v1.4.

## Qué hay de nuevo en la 8.2: órdenes límite en la sandbox y menú ocultable en el escritorio

- **Órdenes límite «vigiladas» en cuentas sandbox.** En un bot **Plan manual** con cuenta, la orden límite ya no se
  queda en papel. KaBor **no la coloca en MyFundedPerps**: vigila el precio en vivo cada segundo y, en cuanto toca tu
  nivel, envía la entrada **a mercado** con el stop y el objetivo del plan, igual que el resto de entradas.
  - Nunca queda una orden huérfana en la cuenta: si el plan se anula, caduca, llega antes al objetivo o reinicias, no
    hay nada que retirar, y la posición siempre nace con su stop.
  - Telegram avisa con el nivel tocado, el precio de entrada y el **deslizamiento** (la diferencia con tu límite).
  - El modelo del bot registra la entrada al mismo precio que la cuenta, así que el panel y la cuenta cuadran.
  - Solo actúa con precio en vivo reciente (menos de 30 s). Si la cuenta rechaza la orden, lo verás en los avisos de
    ejecución y el plan sigue en papel con su orden límite.
  - Por qué no una orden límite real: falta confirmar en la documentación de la API de MyFundedPerps cómo adjuntar el
    stop y el objetivo y cómo cancelarla. Cuando la tengamos, se puede valorar.
- **Menú ocultable en el escritorio.** Botón «Ocultar el menú» (las flechas junto a cerrar sesión): los bots ocupan
  toda la pantalla y arriba queda una barra con el botón para volver a mostrarlo. Se recuerda entre visitas. En el
  móvil sigue como siempre.

Incluye todo desde la 7.7. Actualizar a la 8.2 desde la 7.6 o posterior (si instalas la 8.3, usa sus comandos):

```
cd ~
tar czf kcbot2_copia_antes82.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v8_2.zip -d tmp82
cp tmp82/kcbot2/{kcbot.py,nucleo.py,planes.py,fiscal.py,panel.html,LEEME.md} ~/kcbot2/
cp tmp82/kcbot2/estrategias/plan_manual.py ~/kcbot2/estrategias/
cp tmp82/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp82
cd ~/kcbot2 && ./bot.sh comprobar
./bot.sh reiniciar
```

`./bot.sh comprobar` tiene que dar [ok] en todas las estrategias, con «Plan manual» v1.3.

## Qué hay de nuevo en la 8.1: menú ordenado, aviso de bots detenidos y gráfico de retiros

- **Bots agrupados en el menú:** «Algorítmicos» y «Plan manual», plegables. Cada grupo muestra cuántos bots tiene, el
  R de hoy del grupo y un punto si hay algún bot detenido (rojo) o con posición abierta (dorado). Recuerda si lo
  dejaste plegado; el grupo del bot que estás viendo se abre solo.
- **Aviso de bots detenidos, en todas las páginas:** aparece arriba en cuanto hay algo parado, con el motivo y qué
  hacer:
  - pausado a mano → botón **Reanudar**;
  - error de la estrategia → **Ver bot**;
  - **límite de protección** (pérdida o ganancia diaria o semanal del bot) → muestra cuándo vuelve solo. El botón
    **Reactivar** pregunta por qué lo reactivas y solo se puede pulsar tras 10 segundos: es tu disciplina, no un
    atajo para seguir operando en un mal día;
  - riesgo global o cuenta bloqueada → enlace a su página (no se levantan desde aquí).
  Se puede plegar, y recuerda cómo lo dejaste.
- **Retiros y gastos:** gráfico por meses (retiros y reembolsos, gastos, y la línea del **neto acumulado del año**) y
  **lista de prop firms y proveedores guardados**. Cada una recuerda su método y su comisión habituales **por tipo de
  movimiento**: al elegir MyFundedPerps en un retiro se rellenan «Rise» y «10 $»; en un gasto, el método con el que
  pagas los challenges. Se pueden quitar de la lista.
- **Idiomas:** inglés y francés cubren todo lo nuevo desde la 7.5 (planes, órdenes límite, datos externos, retiros,
  menú y avisos), también el historial de los planes.

Incluye todo desde la 7.7. Actualizar a la 8.1 desde la 7.6 o posterior (si instalas la 8.2, usa sus comandos):

```
cd ~
tar czf kcbot2_copia_antes81.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v8_1.zip -d tmp81
cp tmp81/kcbot2/{kcbot.py,nucleo.py,planes.py,fiscal.py,panel.html,LEEME.md} ~/kcbot2/
cp tmp81/kcbot2/estrategias/plan_manual.py ~/kcbot2/estrategias/
cp tmp81/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp81
cd ~/kcbot2 && ./bot.sh comprobar
./bot.sh reiniciar
```

## Qué hay de nuevo en la 8.0: retiros y gastos para la renta

Nueva sección **Retiros y gastos** en el menú: el registro de tus payouts y de lo que gastas en trading, en euros y
con sus justificantes, para tenerlo listo al hacer la declaración.

- **Movimientos:** retiro (payout), gasto (challenge, reseteo, suscripción, datos y plataformas, formación, equipo…)
  o reembolso (cuando la firma te devuelve el challenge). Fecha, prop firm o proveedor, cuenta, cómo se cobró o pagó
  (Rise, Wise, Revolut, transferencia, cripto, tarjeta…), importe en dólares, comisión y nota.
- **Euros:** si la plataforma ya te paga en euros (Rise lo hace), pon los euros que recibiste: ese es el importe que
  vale, y KaBor calcula el cambio que te aplicaron. Si no, convierte con el **tipo de referencia del BCE** de esa
  fecha (o del último día hábil). El cambio del BCE se guarda siempre como referencia.
- **Justificantes:** adjunta PDF o capturas (hasta 10 MB cada uno) en cada movimiento. Se guardan en
  `data/fiscal/justificantes`.
- **Resumen:** por año y trimestre y por entidad (retiros, reembolsos, gastos y neto), con aviso de los movimientos
  sin importe en euros o sin justificante.
- **Exportar:** CSV para Excel o tu gestor, e informe para imprimir o guardar como PDF desde el navegador.
- **Privacidad:** solo en el panel, con tu inicio de sesión. Nunca por el conector de Claude.

Esto organiza tus datos; cómo tributan los payouts lo debe confirmar tu gestor. **Haz copia de `data/fiscal`** junto
con el resto de `data`: son datos que no se pueden recuperar de otro sitio.

Incluye todo lo de la 7.7, 7.8 y 7.9. Actualizar a la 8.0 desde la 7.6 o posterior (si instalas la 8.1, usa sus comandos):

```
cd ~
tar czf kcbot2_copia_antes80.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v8_0.zip -d tmp80
cp tmp80/kcbot2/{kcbot.py,nucleo.py,planes.py,fiscal.py,panel.html,LEEME.md} ~/kcbot2/
cp tmp80/kcbot2/estrategias/plan_manual.py ~/kcbot2/estrategias/
cp tmp80/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp80
cd ~/kcbot2 && ./bot.sh comprobar
./bot.sh reiniciar
```

## Qué hay de nuevo en la 7.9: los planes no pierden su entrada

**Corregido:** al cambiar la configuración de un bot (por ejemplo, de papel a sandbox), KaBor descartaba la entrada
pendiente. Es lo correcto para los bots con estrategia (una señal calculada con otros ajustes ya no vale), pero en un
bot **Plan manual** hacía perder una entrada que ya había decidido el plan. Pasó con el plan de CRWV del 8-nov.

- Un bot **Plan manual** conserva ahora su entrada pendiente, o su orden límite, al cambiar la configuración, siempre
  que no cambien el símbolo ni la temporalidad. El registro lo indica («se conserva la entrada pendiente del plan
  manual»). Los demás bots siguen como siempre.
- El historial del plan anota siempre la entrada, también cuando la operación abre y cierra en la misma vela
  («Entrada abierta a 46,63 (y cerrada en la misma vela)»), y el conector muestra su resultado en R. Antes ese caso se
  daba por no abierto.
- Al recargar el historial, una orden límite ya colocada no se vuelve a anunciar.

**Importante con el conector de Claude:** cuando una versión añade o cambia herramientas del conector (como la 7.7 y
la 7.8), Claude sigue usando la lista antigua hasta que lo reconectas: Ajustes → Conectores → desconectar y volver a
conectar, y abrir un chat nuevo. La 7.9 no cambia herramientas, pero si vienes de la 7.6 sí tendrás que hacerlo.

Incluye todo lo de la 7.7 y la 7.8. Actualizar a la 7.9 desde la 7.6, 7.7 o 7.8 (si instalas la 8.0, usa sus comandos):

```
cd ~
tar czf kcbot2_copia_antes79.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v7_9.zip -d tmp79
cp tmp79/kcbot2/{kcbot.py,nucleo.py,planes.py,panel.html,LEEME.md} ~/kcbot2/
cp tmp79/kcbot2/estrategias/plan_manual.py ~/kcbot2/estrategias/
cp tmp79/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp79
cd ~/kcbot2 && ./bot.sh comprobar
./bot.sh reiniciar
```

`./bot.sh comprobar` tiene que dar [ok] en todas las estrategias, con «Plan manual» v1.2.

## Qué hay de nuevo en la 7.8: Claude puede revisar tus resultados

El conector con Claude gana tres herramientas de **solo lectura**, para pedirle una revisión de tus bots y de tus
planes desde el chat:

- **kabor_bots**: estado de cada bot (en marcha, posición abierta y su R latente, R de hoy, de la semana y total,
  acierto, profit factor, drawdown, si opera en cuentas), el riesgo global y el vigilante de deriva.
- **kabor_operaciones**: las operaciones cerradas de los bots y las apuntadas a mano, con la nota, las etiquetas y si
  siguieron el plan según tu diario. Horas en hora de España.
- **kabor_estadisticas**: por bot y periodo, las métricas, largos frente a cortos, motivos de salida, horas y días de la
  semana, MFE/MAE medios y, del diario, el resultado de las operaciones que siguieron el plan frente a las que no.
- **kabor_planes** ahora incluye el **resultado en R** de cada plan disparado.

Ejemplos: «revisa mi semana», «¿cómo van mis planes manuales frente a los bots?», «¿en qué horas pierde el bot de
oro?». Nada de esto cambia ajustes ni opera: confirmar planes, tocar bots y cuentas sigue siendo solo desde el panel.

Este paquete incluye también todo lo de la 7.7 (órdenes límite). Actualizar desde la 7.6 o la 7.7 a la 7.8 (si instalas la 7.9, usa sus comandos):

```
cd ~
tar czf kcbot2_copia_antes78.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v7_8.zip -d tmp78
cp tmp78/kcbot2/{kcbot.py,nucleo.py,planes.py,panel.html,LEEME.md} ~/kcbot2/
cp tmp78/kcbot2/estrategias/plan_manual.py ~/kcbot2/estrategias/
cp tmp78/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp78
cd ~/kcbot2 && ./bot.sh comprobar
./bot.sh reiniciar
```

Después del reinicio, en Claude no hay que hacer nada: el conector ve las herramientas nuevas en el siguiente chat.

## Qué hay de nuevo en la 7.7: órdenes límite en los planes

Un plan puede ahora entrar con una **orden límite** en un precio, por ejemplo «corto límite en el swing high». Es la
forma natural de operar un rechazo: entras en el nivel, no al cierre de la vela.

- Nueva condición **«Orden límite en»** (en el panel y en el conector de Claude). Al primer cierre de vela tras
  confirmar el plan, la orden queda puesta: lo verás en el historial del plan («Orden límite colocada») y en Telegram
  («📌 Orden límite»).
- **Se llena** en cuanto una vela toca el precio, al precio límite, sin esperar al cierre. Si una vela abre ya al otro
  lado del límite (un hueco), se llena en la apertura, a ese precio, que es mejor.
- **Se retira sola** si el precio llega al objetivo antes de llenarla, si se cumple la invalidación, si caduca o si
  cancelas el plan.
- **Casos dudosos, siempre del lado prudente:** si la misma vela toca el límite y el objetivo, no se cuenta (no se sabe
  qué fue primero); si toca el límite y el stop, se cuenta como pérdida.
- **Por ahora solo en papel** (desde la 8.2, en cuentas sandbox se vigila y se envía a mercado al tocar el nivel).
- La orden se coloca en el primer cierre de vela tras confirmar: en un bot de 1h puede tardar hasta una hora en quedar
  puesta. Para niveles cercanos al precio, usa un bot «Plan manual» de temporalidad menor (5m o 15m).
- El motor (`nucleo.py`) gana las órdenes límite para cualquier estrategia que las pida. Las demás estrategias dan
  exactamente los mismos resultados que antes (comprobado con tus estrategias).

Actualizar desde la 7.6 a la 7.7 (si instalas la 7.8, usa sus comandos):

```
cd ~
tar czf kcbot2_copia_antes77.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v7_7.zip -d tmp77
cp tmp77/kcbot2/{kcbot.py,nucleo.py,planes.py,panel.html,LEEME.md} ~/kcbot2/
cp tmp77/kcbot2/estrategias/plan_manual.py ~/kcbot2/estrategias/
cp tmp77/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp77
cd ~/kcbot2 && ./bot.sh comprobar
./bot.sh reiniciar
```

`./bot.sh comprobar` tiene que dar [ok] en todas las estrategias, con «Plan manual» v1.1.

## Qué hay de nuevo en la 7.6: datos externos para los backtests

Ahora los backtests pueden ir más atrás de lo que da MyFundedPerps. En **Instrumentos → Datos externos para
backtests** subes un CSV de velas para un instrumento y lo usan todos los bots y estrategias que lo operan.

- **Tienen que ser del mismo mercado que opera el bot.** Al lado del instrumento se ve dónde lo opera MFP (por
  ejemplo, Hyperliquid). Al importar, KaBor compara el archivo con las velas reales de MFP en el tramo común:
  diferencia de precio, correlación de los movimientos y desfase horario (si el CSV está en hora de Madrid o de
  Nueva York, lo detecta y lo corrige). Veredicto:
  - **Coincide**: el mismo mercado (diferencias de céntimos).
  - **Aproximado**: un mercado parecido (otra fuente, spot frente a perpetuo, futuros con base). Se puede usar, pero
    el backtest lo marca: sirve para validar una idea, no para medir el resultado exacto.
  - **Sin verificar**: no hay tramo común con MFP para comparar. Se puede usar con cuidado.
  - **No coincide**: otro mercado. No se deja usar.
- **Formatos:** Binance (con o sin cabecera, fechas en ms o µs), TradingView, MetaTrader (fecha y hora separadas),
  Dukascopy y cualquier CSV con fecha y OHLC. De 1m a 4h; de 1m salen solas las de 3m, 5m, 15m, 30m, 1h, 2h y 4h.
  Hasta 1 millón de velas por archivo (unos 2 años de 1m o 9 de 5m).
- **Uso, por instrumento:** «No usar», «Completar MFP hacia atrás» (velas externas antes de la primera de MFP y,
  desde ahí, MFP) o «Solo los externos» (para comparar). Lo usan los backtests del panel, el optimizador y la
  cartera. El **vigilante de deriva usa siempre MFP**, porque compara el directo con su referencia.
- El resultado del backtest indica la fuente («MFP + datos externos», cuántas velas externas y hasta qué fecha).
- Los filtros de tendencia de temporalidad mayor se construyen con las mismas velas del backtest.
- **Subida:** desde el panel, por partes. Para archivos muy grandes, cópialos por SCP a
  `data/externos/entrada/` y pulsa «Releer» → «Importar para …».

**El límite de velas.** Cada backtest del panel usa como mucho `FP_BT_MAX` velas (60.000; en 5m son unos 7 meses).
Para aprovechar años de datos en 5m, sube `FP_BT_MAX` en `config.env` (por ejemplo a 200.000, unos 2 años de 5m) o
prueba en 30m o 1h. Más velas son más tiempo y más memoria en la Raspberry: súbelo poco a poco y mira cuánto tarda
un backtest antes de lanzar el optimizador, que repite ese tiempo por cada combinación.

**De dónde sacar datos.** Cripto de Binance: data.binance.vision tiene el histórico completo gratis (velas «klines»
del mercado de futuros USDT-M si MFP opera ese perpetuo). Oro y Nasdaq en Hyperliquid son perpetuos recientes: con
XAUUSD spot o futuros saldrá «aproximado» (otro nivel de precio, y el perpetuo cotiza 24/7 mientras el spot cierra
el fin de semana).

También en la 7.6: los diálogos de confirmación de **Planes** no mostraban bien la pregunta. Corregido.

Actualizar desde la 7.3, 7.4 o 7.5 (los bots no se tocan; incluye los idiomas y la cinta de precios de la 7.4 y
los planes de la 7.5):

```
cd ~
tar czf kcbot2_copia_antes76.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v7_6.zip -d tmp76
cp tmp76/kcbot2/{kcbot.py,planes.py,externos.py,deriva.py,panel.html,bot.sh,LEEME.md} ~/kcbot2/
mkdir -p ~/kcbot2/estrategias && cp tmp76/kcbot2/estrategias/plan_manual.py ~/kcbot2/estrategias/
mkdir -p ~/kcbot2/web/i18n
cp tmp76/kcbot2/web/{i18n.js,login.html} ~/kcbot2/web/
cp tmp76/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp76
cd ~/kcbot2 && chmod +x bot.sh && ./bot.sh comprobar
./bot.sh reiniciar
```

Si venías de la 7.3 o la 7.4, lee también los apartados de la 7.4 (idiomas y cinta de precios) y de la 7.5 (Planes y
conector con Claude).

## Qué hay de nuevo en la 7.5: planes manuales y conector con Claude

Tu análisis, ejecutado sin emociones. Le pasas a Claude (en la app o en claude.ai) una captura con tu análisis y tus
condiciones; Claude comprueba los niveles con las velas reales de KaBor, te señala lo que no cuadra y, cuando le das
el visto bueno, crea un **plan pendiente** en el panel → **Planes**. Nada se opera hasta que lo confirmas tú.

- **Estrategia «Plan manual»** (`estrategias/plan_manual.py`): no tiene lógica propia; vigila los planes activos de
  su símbolo y temporalidad. Entrada a mercado en la apertura de la vela siguiente a la que cumple la condición, con
  el stop y el objetivo del plan y el riesgo del bot. Solo actúa en directo (nunca en backtests).
- **Plan** = condición (cierre por encima/debajo de un nivel, o toque), cierres seguidos (1-5), stop, objetivo,
  invalidación opcional y hora límite (máx. 7 días). Se dispara una sola vez.
- **Freno**: un plan activo no se edita. Cancelarlo pide una espera (`FP_PLAN_ESPERA_MIN`, 5 min) y una segunda
  confirmación. Claude no puede confirmar planes, cancelar activos, operar ni cambiar ajustes.
- También puedes crear planes a mano en el panel (botón «Plan a mano»), sin Claude.
- Avisos por Telegram al crear, activar, disparar, invalidar o caducar un plan.

- Nueva sección **Planes** en el menú (debajo de Diario), traducida al inglés y al francés.

Actualizar desde la 7.4 a la 7.5 (si instalas la 7.6, usa sus comandos):

```
cd ~
tar czf kcbot2_copia_antes75.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v7_5.zip -d tmp75
cp tmp75/kcbot2/{kcbot.py,planes.py,panel.html,bot.sh,LEEME.md} ~/kcbot2/
cp tmp75/kcbot2/estrategias/plan_manual.py ~/kcbot2/estrategias/
cp tmp75/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp75
cd ~/kcbot2 && chmod +x bot.sh && ./bot.sh comprobar
./bot.sh reiniciar
```

`./bot.sh comprobar` tiene que mostrar «Plan manual» con [ok]. Luego, en el panel, crea un bot con la estrategia
**Plan manual**, el símbolo y la temporalidad en que quieras ejecutar tus planes, y su riesgo. Un solo bot «Plan
manual» por símbolo y temporalidad, mejor en un símbolo que no opere otro bot en la misma cuenta (las posiciones se
compensarían). Ya puedes usar «Plan a mano»; el conector con Claude es opcional:

### Conector con Claude (opcional)

1. `./bot.sh conector` crea el token secreto (`FP_MCP_TOKEN` en `config.env`) y muestra la ruta `/mcp/<token>`.
   `./bot.sh reiniciar`.
2. Los servidores de Anthropic tienen que llegar a esa ruta por HTTPS público. Con Tailscale Funnel, publicando
   **solo** `/mcp` y en el puerto **8443** (el 443 ya lo usa `tailscale serve` para el panel; si activaras Funnel en
   el 443, el panel entero quedaría público):

       sudo tailscale funnel --bg --https=8443 --set-path=/mcp http://127.0.0.1:8083/mcp

   La primera vez Tailscale te dará un enlace para permitir Funnel en tu red. Comprueba con
   `tailscale funnel status` que solo aparece `/mcp`. Desde el móvil con datos (fuera de Tailscale), abre
   `https://NOMBRE.TU-RED.ts.net:8443/` → no debe mostrar el panel; `…:8443/mcp/<token>` → «405».
3. En Claude: Ajustes → Conectores → Añadir conector personalizado. URL:
   `https://NOMBRE.TU-RED.ts.net:8443/mcp/<token>`, sin OAuth. El token va en la URL: no la compartas.
4. En un chat: «Aquí va mi análisis de oro en 5m [captura]. Si cierra por encima del FVG en 2648,5, largo hasta el
   high de 2662, stop bajo el FVG; se anula si cierra por debajo de 2639; válido hasta el cierre de Nueva York.»

Si se filtra el token: borra la línea `FP_MCP_TOKEN` de `config.env`, `./bot.sh conector`, `./bot.sh reiniciar` y
cambia la URL del conector. Para desactivarlo: `sudo tailscale funnel --https=8443 off`.

## Qué hay de nuevo en la 7.4

**Idiomas.** En Ajustes → «Idioma y pantalla» eliges el idioma del panel: **español, inglés o francés**.

- Se traduce todo lo que se ve: menús, tarjetas, formularios de las estrategias con sus ayudas, el simulador, el
  optimizador, la cartera, el vigilante, el diario, los avisos de la pantalla, las gráficas y la pantalla de inicio
  de sesión. Números y fechas siguen el formato del idioma (1,234.50 en inglés).
- Se guarda en cada navegador o móvil: puedes tener el móvil en español y el ordenador en inglés. La primera vez se
  elige solo según el idioma del navegador.
- Lo que escribes tú (notas del diario, etiquetas, nombres de bots) no se toca.
- Los mensajes de **Telegram siguen en español** por ahora.
- Cómo funciona: el panel sigue escrito en español y `web/i18n.js` traduce en pantalla con los diccionarios
  `web/i18n/en.json` y `web/i18n/fr.json` (unas 1.800 frases cada uno). Añadir otro idioma es añadir un diccionario.
  Si alguna frase sale en español, mándame una captura y la añado.

**Cinta de precios.** Arriba del todo, una cinta con cada instrumento que operan tus bots: precio, variación en
24 horas, una minigráfica del día y un ▲/▼ si algún bot tiene posición abierta. Si no caben todos, se desplaza sola
(se para al poner el dedo o el ratón encima). Toca un instrumento para ir a su bot.

- **No gasta peticiones a la API:** el precio sale del mismo flujo en directo que ya usan los bots y la variación de
  las velas que tienen en memoria.
- Se puede quitar en Ajustes → «Idioma y pantalla» → «Cinta de precios arriba».

Actualizar desde la 7.3 (los bots no se tocan):

```
cd ~
tar czf kcbot2_copia_antes74.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v7_4.zip -d tmp74
cp tmp74/kcbot2/{kcbot.py,panel.html,LEEME.md} ~/kcbot2/
mkdir -p ~/kcbot2/web/i18n
cp tmp74/kcbot2/web/{login.html,i18n.js} ~/kcbot2/web/
cp tmp74/kcbot2/web/i18n/*.json ~/kcbot2/web/i18n/
rm -r tmp74
cd ~/kcbot2 && grep -m1 '^VERSION' kcbot.py
./bot.sh reiniciar
```

Si vienes de la 7.2 o anterior, copia también lo de la 7.3 (van en el zip: `acceso.py`, `diario.py`, `bot.sh`,
`web/sw.js`, `web/manifest.webmanifest`, `web/icono-192.png`) y lo de versiones anteriores (`deriva.py`, `avisos.py`,
`cartera.py`, `optimizador.py`).

## Qué hay de nuevo en la 7.3

**Diario de operaciones.** Nueva sección «Diario» en el menú, debajo de Estadísticas.

- Todas las operaciones de todos los bots (también los archivados) en una lista por días, con su resultado en R y en $
  si el bot tiene riesgo en $.
- Toca una operación para escribirle una **nota**, ponerle **etiquetas** (hasta 8: «setup a+», «fomo», «noticia»…)
  y marcar si fue **según el plan o fuera del plan**.
- **Operaciones a mano:** con «Apuntar operación manual» apuntas lo que operas tú, como un swing en CL: instrumento,
  lado, fechas y resultado en R, en $ o en los dos.
- Arriba, el resumen del periodo: resultado según el plan frente a fuera del plan (lo que de verdad cuesta saltarse el
  plan) y una tabla por etiqueta con operaciones, R y acierto. Se puede filtrar por bot, por etiqueta o ver solo lo
  que falta por anotar.
- Las operaciones de los bots no se copian ni se tocan: el diario solo guarda tus notas, en `data/diario.json`.

**Verificación en dos pasos.** En Ajustes → «Verificación en dos pasos».

- Al activarla, para entrar al panel hace falta la contraseña y un código de 6 cifras de una app del móvil (Google
  Authenticator, Aegis, Microsoft Authenticator, 1Password…). En el móvil, un botón abre la app directamente; en el
  ordenador sale un código QR.
- Al activarla te da **8 códigos de recuperación** de un solo uso, por si pierdes el móvil. Guárdalos fuera del móvil.
- Un código ya usado no vale dos veces, y los códigos fallidos cuentan para el bloqueo de 15 minutos, como las
  contraseñas.
- Si pierdes el móvil y los códigos, en la Raspberry: `./bot.sh quitar-2fa USUARIO`.
- Cambiar la contraseña desde el panel no pide código: ya estás dentro.

**App en el móvil.** En Ajustes → «App en el móvil».

- El panel se puede instalar como una app: icono en la pantalla de inicio, pantalla completa sin barra del navegador
  y accesos directos (mantén pulsado el icono) a Vigilante, Cartera y Estadísticas.
- Si la Raspberry no responde, sale una pantalla clara de «sin conexión» en vez del error del navegador.
- No guarda datos en el móvil: siempre ves lo que dice la Raspberry en ese momento.
- **Importante:** el navegador solo deja instalarla con un candado limpio. Entrando por la IP de casa con el
  certificado propio (el candado tachado), no se puede. Entra por **Tailscale** (ver más abajo, «Alternativa sin
  avisos y desde fuera de casa») y desde ahí: Ajustes → «Instalar app», o el menú del navegador → «Instalar app».

Actualizar desde la 7.2 (cambian varios archivos, algunos nuevos; los bots no se tocan):

```
cd ~
tar czf kcbot2_copia_antes73.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v7_3.zip -d tmp73
cp tmp73/kcbot2/{kcbot.py,acceso.py,diario.py,panel.html,bot.sh,LEEME.md} ~/kcbot2/
cp tmp73/kcbot2/web/{login.html,sw.js,manifest.webmanifest,icono-192.png} ~/kcbot2/web/
rm -r tmp73
cd ~/kcbot2 && grep -m1 '^VERSION' kcbot.py
./bot.sh reiniciar
```

Si vienes de la 7.1 o anterior, copia también `deriva.py`, `avisos.py`, `cartera.py` y `optimizador.py` (van en el
zip).

## Qué hay de nuevo en la 7.2

**Vigilante de deriva.** Nueva sección «Vigilante» en el menú, debajo de Cartera, y una tarjeta en el panel de cada
bot. Responde a la pregunta del día a día: ¿este bot está haciendo lo que prometía su backtest, o algo ha cambiado?

- **Cómo funciona.** Para cada bot en marcha hace un backtest de referencia con sus ajustes actuales (90 días por
  defecto) y lo rehace cuando cambias los ajustes o cada semana. Cada 10 minutos coge las operaciones reales en papel
  hechas con esa configuración y fabrica miles de rachas posibles del backtest con el mismo número de operaciones.
  Luego mira dónde cae lo real.
- **Qué mira:** el resultado acumulado, las pérdidas seguidas, el drawdown y la frecuencia. Si opera mucho más o mucho
  menos de lo esperado, suele ser que no llegan bien los datos o que el mercado ha cambiado.
- **Estados:** «como en el backtest», «a vigilar» (raro, pero puede ser mala suerte), «en deriva» (muy improbable si
  la estrategia siguiera funcionando igual) y «pocas operaciones» (hasta 10, no opina).
- **El cono.** Cada bot tiene una gráfica con las bandas donde caen el 90 % y el 50 % de las rachas del backtest, y
  encima la línea real. Si la línea se sale por abajo, el bot se está apartando.
- **Avisos por Telegram** cuando un bot empeora de estado y cuando vuelve a la normalidad. Es un evento nuevo en
  Avisos, «Vigilante de deriva», activado por defecto.
- **Pausa automática (opcional).** Si la activas, al entrar en deriva el bot deja de abrir operaciones nuevas. No cierra
  la que tenga abierta; se reanuda a mano desde el panel.
- En el menú, «Vigilante» muestra cuántos bots están en deriva (en rojo) o a vigilar.
- Se puede cambiar el periodo de la referencia y a partir de cuántas operaciones opina. «Rehacer referencia» repite el
  backtest de un bot cuando quieras.

Ojo: es estadística, no adivinación. «A vigilar» salta a veces por pura mala suerte. «En deriva» con muchas
operaciones sí es una señal seria de que la estrategia ya no se comporta como en el histórico.

Actualizar desde la 7.1 (cambian cinco archivos, uno nuevo; los bots no se tocan):

```
cd ~
tar czf kcbot2_copia_antes72.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v7_2.zip -d tmp72
cp tmp72/kcbot2/{kcbot.py,deriva.py,avisos.py,panel.html,LEEME.md} ~/kcbot2/
rm -r tmp72
cd ~/kcbot2 && grep -m1 '^VERSION' kcbot.py
./bot.sh reiniciar
```

Si vienes de la 7.0 o anterior, copia también `cartera.py` y `optimizador.py` (van en el zip). El vigilante hace su
primera revisión un par de minutos después de reiniciar.

## Qué hay de nuevo en la 7.1

**Cartera: varios bots juntos, como en la misma cuenta.** Nueva sección «Cartera» en el menú, debajo de Estadísticas.

- **Qué hace.** Marca los bots que quieras juntar y pulsa «Calcular cartera». Cada bot usa sus ajustes guardados y sus
  operaciones se juntan en una sola cuenta, todo en R. El **peso** multiplica el riesgo de cada bot: ×0,5 es la mitad.
- **Dos fuentes.** «Histórico» prueba cada bot con el periodo elegido y usa el tramo que tienen todos en común.
  «Operaciones reales» usa lo que han hecho de verdad en papel desde que arrancaron, y es instantáneo.
- **Lo que no se ve bot a bot.** El drawdown conjunto frente a la suma de los drawdowns de cada bot (la
  diversificación), el peor y el mejor día de la cuenta, los días en positivo, cuántas operaciones llegan a estar
  abiertas a la vez y cuánto riesgo suman en ese momento.
- **Curva** de la cartera en dorado, con la de cada bot detrás, y una tabla bot a bot con lo que aporta cada uno.
- **Correlación.** Un mapa dice si los bots ganan y pierden los mismos días. Cerca de 0 o negativa, se compensan; cerca
  de 1, sus rachas malas se suman. El panel avisa si dos bots se mueven juntos, si alguno resta o si llega a haber
  mucho riesgo abierto a la vez.
- **Simulador de prop firm con la cartera.** Debajo aparece el mismo simulador Monte Carlo, ahora con las operaciones
  de todos los bots juntos: la forma de saber si la cuenta entera pasa el challenge. Al volver a la configuración de un
  bot, el simulador vuelve a usar la prueba de ese bot.
- Corre en segundo plano en la Raspberry, como el optimizador: con «Histórico» tarda lo que un «Probar en el
  histórico» por cada bot.

**Arreglos.**

- En el mapa del optimizador, el nombre del eje vertical ya no se monta encima de los valores; el margen se ajusta a
  las etiquetas.
- En el móvil, algunos campos dentro de los apartados plegables (formulario del bot y simulador) se salían un poco por
  la derecha. Ya no.

Actualizar desde la 7.0 (cambian cuatro archivos, uno nuevo; los bots no se tocan):

```
cd ~
tar czf kcbot2_copia_antes71.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v7_1.zip -d tmp71
cp tmp71/kcbot2/{kcbot.py,cartera.py,panel.html,LEEME.md} ~/kcbot2/
rm -r tmp71
cd ~/kcbot2 && grep -m1 '^VERSION' kcbot.py
./bot.sh reiniciar
```

Si vienes de la 6.9 o anterior, copia también `optimizador.py`.

## Qué hay de nuevo en la 7.0

**Optimizador con walk-forward.** En la configuración de cada bot, debajo de «Probar en el histórico», hay una tarjeta
nueva: «Optimizar ajustes». Sirve para buscar mejores valores sin engañarte a ti mismo.

- **Qué hace.** Elige uno o dos ajustes de la estrategia (con desde, hasta y paso; o las opciones de una lista) y prueba
  todas las combinaciones, hasta 225, con el periodo elegido arriba. Usa los demás valores del formulario, aunque no
  estén guardados. El bot en marcha no se toca.
- **Mapa.** Con dos ajustes sale un mapa de colores; con uno, barras. La estrella es la mejor casilla y el recuadro
  dorado es la «zona robusta»: la casilla que mejor sale de media con sus vecinas. Si la estrella está aislada entre
  casillas malas, es casualidad; la zona robusta suele aguantar mejor en real. Toca una casilla para ver sus números y
  cargarla en el formulario.
- **Walk-forward.** El periodo se parte en tramos. En cada uno se eligen los mejores ajustes usando solo lo anterior y se
  mide qué hacen en el tramo siguiente, que no han visto. La suma de esos tramos es lo que habrías ganado de verdad
  optimizando así, y el veredicto lo resume: robusta, dudosa o sobreajustada. También verás lo que hacen tus ajustes
  actuales en los mismos tramos, para comparar.
- **Qué maximizar:** R / drawdown (por defecto, premia ganar sin sufrir), R total, profit factor o SQN. «Mín.
  operaciones» descarta combinaciones con muy pocas operaciones, que salen bien por suerte.
- Corre en segundo plano en la Raspberry: puedes salir de la pantalla y volver. Si tarda más de un minuto y tienes
  Telegram activado, te avisa al terminar. Solo una optimización a la vez; se puede cancelar.
- Cada combinación tarda lo que un «Probar en el histórico». En la Raspberry, cuenta con alrededor de un segundo por
  combinación y por cada 10.000 velas: 100 combinaciones con 30 días de 5m llevan unos minutos.

**Simulador de prop firm, mejorado.**

- El abanico se corta cuando ya se ha decidido casi todo (el 99 % de las cuentas): con plazos largos ya no se queda
  aplastado.
- La gráfica de riesgo muestra encima de cada punto los días típicos para pasar.
- Con plazos de más de 90 días, para recomendar el riesgo cuenta como aprobadas solo las cuentas que pasan en 60 días o
  menos. Así no recomienda un riesgo diminuto que tarda meses en pasar.

**Cómo usarlo bien:** optimiza con todo el historial, carga la zona robusta (no la estrella), pruébala en el histórico y
pásala por el simulador de prop firm. Si el walk-forward sale «sobreajustada», no cambies los ajustes: la estrategia no
mejora tocando números.

Actualizar desde la 6.9 (cambian cuatro archivos, uno nuevo; los bots no se tocan):

```
cd ~
tar czf kcbot2_copia_antes70.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v7_0.zip -d tmp70
cp tmp70/kcbot2/{kcbot.py,optimizador.py,panel.html,LEEME.md} ~/kcbot2/
rm -r tmp70
cd ~/kcbot2 && grep -m1 '^VERSION' kcbot.py
./bot.sh reiniciar
```

Después, recarga el panel en el navegador.

## Qué hay de nuevo en la 6.9

**Simulador de prop firm (Monte Carlo).** Debajo del resultado de «Probar en el histórico» hay un apartado nuevo que
responde a la pregunta importante: con esta estrategia y estas reglas, ¿cuántas veces pasaría el challenge y cuántas
rompería la cuenta?

- **Cómo funciona.** Coge los días de la prueba (también los días sin operaciones, con sus operaciones en orden y ya
  con comisiones) y los baraja miles de veces para fabricar cuentas posibles. En cada una aplica las reglas día a día:
  pérdida diaria desde el saldo del inicio del día, pérdida máxima fija, dinámica o dinámica hasta el saldo inicial,
  objetivo, días mínimos con operaciones y plazo.
- **Plantillas** para empezar (fase 1, fase 2, fondeada un mes y trailing tipo futuros). Son genéricas: ajusta los
  números a las reglas exactas de tu cuenta. El panel recuerda los últimos valores en ese navegador.
- **Resultados:** probabilidad de pasar (o de sobrevivir, con objetivo 0), de romper la pérdida diaria y la máxima, días
  típicos para pasar, intentos esperados, drawdown típico y del peor 5 %, y resultado al final del plazo en % y en $.
- **Abanico:** cómo evoluciona la cuenta en el 90 % de los casos, la mitad central y el caso típico, con el objetivo y
  el límite marcados.
- **¿Qué riesgo conviene?** Repite la simulación con riesgos de 0,25 % a 2 % y propone el que más pasa sin romper la
  cuenta en más del 10 % de los casos (en una fondeada, el riesgo más alto que rompe el 5 % o menos).
- **Barajar en bloques de 5 días** conserva las rachas buenas y malas seguidas: es más exigente y más realista que
  barajar días sueltos.
- Todo se calcula en el navegador: la Raspberry no trabaja más. Cuenta solo operaciones cerradas, así que con el flotante
  una regla puede romperse algo antes. Con pocos días de prueba o una estrategia que pierde, el panel avisa.

Actualizar desde la 6.8.1 (cambian tres archivos; los bots no se tocan):

```
cd ~
tar czf kcbot2_copia_antes69.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v6_9.zip -d tmp69
cp tmp69/kcbot2/{kcbot.py,panel.html,LEEME.md} ~/kcbot2/
rm -r tmp69
cd ~/kcbot2 && grep -m1 '^VERSION' kcbot.py
./bot.sh reiniciar
```

Después, recarga el panel en el navegador (en el móvil, cierra la pestaña y vuelve a abrirla) y haz una prueba nueva:
las pruebas hechas con la versión anterior no traen los datos día a día.

## Qué hay de nuevo en la 6.8.1

**Los comandos de contexto eligen el bot como lo ves en el panel.** Hasta ahora, `./bot.sh contexto 1` usaba la carpeta
interna `data/bots/1`, que es el primer bot que creaste (aunque esté archivado), no el primero del menú.

- **El número es la posición en el menú del panel**: `./bot.sh contexto 1` es el primer bot del menú lateral.
- **También vale el nombre**, completo o una parte que no se repita: `./bot.sh contexto "Oro 5m"`,
  `./bot.sh analizar oro`. Si la parte coincide con un bot activo y con otro archivado, se usa el activo. Los bots
  archivados se eligen por su nombre.
- **`./bot.sh bots`** lista los bots con su número, mercado, temporalidad y estrategia.
- Cada comando dice primero qué bot está usando, y el CSV exportado lleva su nombre
  (`contexto_backtest_Oro_5m_XAU_5m_….csv`).
- `./bot.sh analizar 1` analiza el `contexto.csv` en directo del primer bot del menú; `./bot.sh analizar ARCHIVO.csv`
  sigue igual.

Actualizar desde la 6.8 (solo cambian cuatro archivos; los bots no se tocan):

```
cd ~
tar czf kcbot2_copia_antes681.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v6_8_1.zip -d tmp681
cp tmp681/kcbot2/{kcbot.py,contexto.py,bot.sh,LEEME.md} ~/kcbot2/
rm -r tmp681
cd ~/kcbot2 && chmod +x bot.sh && grep -m1 '^VERSION' kcbot.py
./bot.sh reiniciar
./bot.sh bots
```

## Qué hay de nuevo en la 6.8

**Contexto de cada operación**: el primer paso para que los bots aprendan de sus errores. Cada operación guarda una
«foto» del mercado en la vela de la señal y, al cerrarse, se anota en `contexto.csv` (en la carpeta del bot y en el
informe ZIP) junto con su resultado.

- **Qué se anota**: hora, día y sesión; volatilidad (ATR en %, ATR frente a su media); la vela de la señal (rango,
  cuerpo, dónde cierra); tendencia y momento (distancia y pendiente de las EMA 50/200, ADX, RSI, eficiencia de
  Kaufman, posición en el rango de 50 velas, racha); la distancia a cada línea de la estrategia en ATR (bandas de
  Keltner, media…); stop en ATR, coste en R y hueco de apertura; lo que venía pasando (velas desde la última
  operación, R de las 3 anteriores, pérdidas seguidas, R y operaciones del día) y, en directo, las operaciones
  abiertas en los otros bots y el resultado del día del riesgo global. Todo lo direccional va «a favor» del lado
  que se opera (también con señales invertidas, que quedan marcadas en la columna `invertida`).
- **Nada del futuro**: solo la vela de la señal y las anteriores.
- **No cambia ninguna decisión**: con los mismos ajustes, los bots dan exactamente las mismas operaciones que en la
  6.7 (comprobado con tus 9 estrategias, en 5m y 1h, sin invertir, «solo dirección» y «espejo exacto»).
- **Historial desde hoy, sin esperar**: el contexto también sale del backtest. `./bot.sh contexto 1 --dias 365`
  hace el backtest con los ajustes del primer bot del menú y guarda el contexto de cada operación en
  `data/contexto_backtest_….csv`. También `./bot.sh probar ARCHIVO … --exportar`. El backtest está limitado a
  `FP_BT_MAX` velas (60.000): en 5m son unos 200 días; súbelo en `config.env` para más.
- **`./bot.sh analizar N`** (o `./bot.sh analizar ARCHIVO.csv`): parte cada variable en 5 tramos y te dice en
  cuáles se gana y se pierde. El mejor y el peor tramo se eligen con la **primera mitad** del historial y se
  comprueban en la **segunda**, que no se ha usado para elegirlos: «CONFIRMADO» (t ≥ 3), «probable» (t ≥ 2),
  «apunta» o «no se confirma». Si el bot opera en sandbox, compara además el R real con el del modelo.

**Cómo usarlo bien.** Con pocos cientos de operaciones casi todo es ruido: en las pruebas, un efecto moderado se
detecta la mitad de las veces con 460 operaciones y siempre con 1.400. Una variable confirmada no es todavía un
filtro: pruébala como filtro en un backtest de otro periodo antes de tocar un bot. Más adelante, con datos de sobra,
este archivo es lo que alimentará un filtro automático de señales (meta-labeling).

### Al actualizar

Desde la 6.7 (no cambia el formato del estado; tus bots, su historial y sus operaciones abiertas siguen igual):

```
cd ~
tar czf kcbot2_copia_antes68.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v6_8_contexto.zip -d tmp68
cp tmp68/kcbot2/{kcbot.py,nucleo.py,contexto.py,bot.sh,LEEME.md} ~/kcbot2/
rm -r tmp68
cd ~/kcbot2 && chmod +x bot.sh && grep -m1 '^VERSION' kcbot.py
./bot.sh comprobar
./bot.sh reiniciar
```

Las operaciones que ya estaban abiertas al actualizar se cierran sin contexto; las siguientes ya lo llevan.

**Para volver a la 6.7**: `./bot.sh parar`, `cd ~ && rm -rf kcbot2 && tar xzf kcbot2_copia_antes68.tgz` y
`cd kcbot2 && ./bot.sh arrancar`.

## Qué hay de nuevo en la 6.7

**Invertir señales** (Configuración → Mercado → «Invertir señales»), para probar si una señal que falla tiene valor al
revés:

- **Espejo exacto**: opera el lado contrario con el stop donde estaría el objetivo y el objetivo donde estaría el
  stop. Sin break-even, trailing, ampliación ni salidas de la estrategia (la duración máxima sí se aplica, porque es
  simétrica). Solo funciona con objetivo fijo o RR; con objetivo dinámico las señales se descartan con el motivo
  «espejo sin objetivo».
- **Solo dirección**: lado contrario con el stop a la misma distancia al otro lado y toda la gestión normal. Ya no es
  el espejo: es otra estrategia.
- Los filtros (dirección, horario, tendencia, noticias, stop mínimo y máximo) se aplican al lado que se opera.
- Si la vela toca el stop y el objetivo a la vez, se sigue asumiendo el stop, así que el espejo es algo más
  conservador que el original.

**Ojo con las R**: en el espejo el riesgo es la distancia hasta el antiguo objetivo (con RR 2, el doble de stop), así
que el R total no es el del original con el signo cambiado. Compara el **profit factor** y el **R sin comisiones**.

**R sin comisiones** en el backtest (y en cada operación del visor): si el original pierde sin comisiones, el espejo
gana sin comisiones; si el original solo pierde por las comisiones, invertir no arregla nada porque las comisiones se
pagan igual. En el visor y en el aviso de señal de Telegram se marca cuándo la señal es invertida.

Los resultados de los bots y backtests sin invertir no cambian.

### Al actualizar

Desde la 6.3 o posterior (el zip lo incluye todo):

```
cd ~
tar czf kcbot2_copia_antes67.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v6_7.zip -d tmp67
cp tmp67/kcbot2/{kcbot.py,nucleo.py,noticias.py,avisos.py,panel.html,LEEME.md} ~/kcbot2/
rm -r tmp67
cd ~/kcbot2 && ./bot.sh comprobar
./bot.sh reiniciar
```

**Para volver atrás**: `./bot.sh parar`, `cd ~ && rm -rf kcbot2 && tar xzf kcbot2_copia_antes67.tgz` y
`cd kcbot2 && ./bot.sh arrancar`.

## Qué hay de nuevo en la 6.6

**Visor de operaciones.** Tras un backtest, debajo de la curva aparece la **lista de operaciones** (las últimas 1.000),
con filtro: todas, ganadoras, perdedoras, largos o cortos. Al tocar una se abre un **gráfico de la operación** dentro
del panel:

- Velas con las líneas de la estrategia (por ejemplo los canales de Keltner), unas 80 velas antes de la entrada y 40
  después de la salida.
- La operación dibujada como la herramienta de posición: zona verde hasta el objetivo, roja hasta el stop, la entrada,
  la salida con su resultado en R y, si la hubo, la venta parcial.
- **El recorrido del stop y del objetivo** vela a vela (break-even, trailing, ampliaciones, objetivo dinámico).
- Debajo, los datos: entrada, salida, stop inicial y final, objetivo, motivo, duración, recorrido a favor y en contra
  (MFE / MAE), gestión y **la tendencia que leía el filtro al entrar**.
- Arrastra para moverte, rueda del ratón o pellizco para el zoom, «Ajustar a la operación» y «Ver todo». ‹ › (o las
  flechas del teclado) pasan a la operación anterior o siguiente; Esc cierra. En el móvil ocupa toda la pantalla.

También funciona en la tabla de operaciones del panel de cada bot. Las operaciones en directo guardan el recorrido del
stop y la tendencia desde esta versión; en las anteriores se ve el stop inicial.

El gráfico usa las velas guardadas en la Raspberry (`data/cache/`), así que las operaciones muy antiguas que ya no
estén guardadas no se pueden dibujar. Los resultados de los backtests no cambian.

### Al actualizar

Desde la 6.3, 6.4 o 6.5 (el zip las incluye todas):

```
cd ~
tar czf kcbot2_copia_antes66.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v6_6.zip -d tmp66
cp tmp66/kcbot2/{kcbot.py,nucleo.py,noticias.py,avisos.py,panel.html,LEEME.md} ~/kcbot2/
rm -r tmp66
cd ~/kcbot2 && ./bot.sh comprobar
./bot.sh reiniciar
```

**Para volver atrás**: `./bot.sh parar`, `cd ~ && rm -rf kcbot2 && tar xzf kcbot2_copia_antes66.tgz` y
`cd kcbot2 && ./bot.sh arrancar`.

## Qué hay de nuevo en la 6.5

El **filtro de tendencia** de cada bot (Configuración → «Filtro de tendencia») se amplía. Sigue siendo común a todas
las estrategias y funciona igual en directo, en el backtest del panel y en `./bot.sh probar`.

- **Métodos nuevos** (además de RSI, EMA y RSI y EMA, que dan exactamente lo mismo que antes):
  - **EMA con pendiente**: alcista con el precio sobre la EMA *y* la EMA subiendo respecto a hace N velas.
  - **Dos EMAs**: la rápida sobre la lenta (por ejemplo 50 y 200).
  - **ADX + DI**: hay tendencia si el ADX supera el mínimo (20 por defecto); su dirección la dan DI+ y DI−.
  - **Estructura**: pivotes de máximos y mínimos. Alcista con máximos y mínimos crecientes y el precio sin perder el
    último mínimo; bajista al revés. Si no, estructura mixta.
- **Zona neutra en ATR**: el precio (o la EMA rápida) tiene que estar al menos X ATR de la EMA de la temporalidad mayor;
  más cerca no hay tendencia. **En zona neutra** eliges: sin entradas (por defecto) o dejar pasar los dos lados.
- **Confirmación con otra temporalidad** (grupo «Tendencia: confirmación»): un segundo filtro, con su propio método,
  que tiene que estar de acuerdo. Por ejemplo 4h con EMA 50 y 1h con EMA 200, o 4h con ADX y la misma 4h con EMA.
- **Temporalidad diaria (1d)**: días UTC construidos con velas de 4h.
- **Se ve la tendencia**: en la tarjeta de cada bot del Resumen (▲ alcista, ▼ bajista, ◆ neutra), en el panel del bot
  con el detalle (por ejemplo «4h ▲ alcista · EMA 50 sobre EMA 200»), en `/estado` y en el aviso de señal de Telegram.
- **Motivos separados** en las señales filtradas: tendencia en contra, zona neutra, calentando y los mismos de la
  confirmación. Así el backtest te dice cuántas señales quita cada cosa.
- **Viene desactivado** (como siempre, «Filtro: no»). Tus bots no cambian hasta que lo actives. Cambiar cualquier
  ajuste de tendencia recarga el historial del bot.

**Antes de usarlo en un bot, pruébalo en el backtest** con y sin filtro: Keltner + RSI entra en sobrecompra y
sobreventa, y un filtro de tendencia le quita justo los rebotes contra tendencia.

### Al actualizar

Desde la 6.3 (si no instalaste la 6.4, este zip la incluye):

```
cd ~
tar czf kcbot2_copia_antes65.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v6_5.zip -d tmp65
cp tmp65/kcbot2/{kcbot.py,nucleo.py,noticias.py,avisos.py,panel.html,LEEME.md} ~/kcbot2/
rm -r tmp65
cd ~/kcbot2 && ./bot.sh comprobar
./bot.sh reiniciar
```

**Para volver atrás**: `./bot.sh parar`, `cd ~ && rm -rf kcbot2 && tar xzf kcbot2_copia_antes65.tgz` y
`cd kcbot2 && ./bot.sh arrancar`.

## Qué hay de nuevo en la 6.4

Nueva sección **Noticias** en el menú lateral (Herramientas): un **filtro de noticias** para todos los bots.

- **Sin entradas alrededor de cada noticia.** Si la entrada de una señal (la apertura de la vela siguiente) cae dentro
  de la ventana de una noticia, la señal se ignora y queda anotada en la actividad del bot con el motivo
  («noticia: CPI m/m (USD, alto) a las 14:30…»), igual que con el riesgo global. **Las operaciones abiertas no se
  tocan**: siguen con su stop y su objetivo.
- **Calendario**: el feed semanal público de Forex Factory (gratis, sin clave). Se descarga cada hora en segundo plano
  y se guarda en `data/cache/calendario.json`, así que un reinicio no lo vuelve a pedir. No usa ningún modelo de IA.
- **Qué noticias**: por divisa (por defecto **USD**, que mueve oro, Nasdaq, petróleo y BTC) y por impacto (por defecto
  solo **alto**). Puedes ignorar las que contengan ciertas palabras (p. ej. «Bond Auction»).
- **Ventana**: 15 min antes y 15 después por defecto. Las **noticias grandes** (FOMC, tipos de la Fed, Non-Farm, CPI,
  discursos del presidente de la Fed; la lista se edita) tienen su propia ventana: 30 antes y 60 después. Las
  noticias que salen a la misma hora se unen en una sola ventana.
- **Por bot**: eliges a qué bots se aplica.
- **Si el calendario no se puede descargar** durante más de 36 h: seguir operando sin filtro (por defecto) o bloquear
  todas las entradas. A las 6 h sin poder descargarlo te llega un aviso de «Problemas».
- **Telegram**: aviso nuevo **«Noticias»** (X minutos antes de cada ventana, 30 por defecto), con la previsión, el dato
  anterior y los bots que tienen una operación abierta. Comando nuevo `/noticias` (próximas 48 h) y una línea en
  `/estado`.
- `./bot.sh comprobar` revisa también la descarga del calendario.
- **Viene desactivado.** Actívalo en el panel → Noticias → Configurar.
- **No afecta a los backtests** del panel ni a `./bot.sh probar`: no hay calendario histórico.

### Al actualizar

Solo cambian `kcbot.py`, `avisos.py`, `panel.html` y `LEEME.md`, y hay un archivo nuevo, `noticias.py`. Tus bots,
cuentas y ajustes no cambian.

```
cd ~
tar czf kcbot2_copia_v63.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v6_4.zip -d tmp64
cp tmp64/kcbot2/{kcbot.py,noticias.py,avisos.py,panel.html,LEEME.md} ~/kcbot2/
rm -r tmp64
cd ~/kcbot2 && ./bot.sh comprobar     # debe salir «[ok] Calendario de noticias: … eventos esta semana»
./bot.sh reiniciar
```

Después: panel → **Noticias** → Configurar → «Filtro de noticias activo» → Guardar. Y en **Avisos**, activa «Noticias».

**Para volver a la 6.3**: `./bot.sh parar`, `cd ~ && rm -rf kcbot2 && tar xzf kcbot2_copia_v63.tgz` y
`cd kcbot2 && ./bot.sh arrancar`.

## Qué hay de nuevo en la 6.3

Nueva sección **Cuentas** en el menú lateral (Herramientas).

- **Varias cuentas, cada una con su clave de API.** Nombre, clave `fp_test_`, la cuenta de MyFundedPerps (el botón
  «Buscar las cuentas de esta clave» te las lista para elegir), un **multiplicador de riesgo** (0,5 = la mitad del
  riesgo del bot en esa cuenta) y un interruptor para desactivarla. Hasta 10 cuentas. Cada clave de test ve todas tus
  cuentas sandbox, así que con una sola clave puedes dar de alta varias, una por ID.
- **Interruptor «Enviar órdenes»** en el panel. Sustituye a `FP_EXEC` de `config.env`.
- **Cada bot elige sus cuentas** (Configuración → Ejecución en sandbox → Cuentas): un bot por cuenta, o un bot que
  envía la misma señal a varias. Cada orden se calcula con el balance de su cuenta. En cada cuenta, un solo bot por
  mercado. Si quitas una cuenta de un bot con una posición abierta en ella, el bot la sigue gestionando hasta que se
  cierre y después la suelta.
- **Bloqueo de la cuenta**:
  - a mano desde el panel («Bloquear…»: hasta mañana o de 1 a 24 horas) o por Telegram (`/bloquear 1`, `/bloquear 1 4`);
  - automático al llegar a un límite del día: **pérdida** o **ganancia** en $, **% del daily loss** de MyFundedPerps
    consumido u **operaciones** abiertas por los bots;
  - opcional: **cerrar posiciones y cancelar órdenes** al bloquear (cierra todo lo de la cuenta, también lo abierto a
    mano), como el Trader Lockout de la web;
  - **estricto**: no se puede quitar antes de tiempo. Sin estricto, al desbloquear a mano los límites automáticos no
    vuelven a saltar hasta el día siguiente.
  - El día empieza a las **00:00 de Nueva York** (cuando MyFundedPerps reinicia el daily loss), con el cambio de hora
    incluido: son las 06:00 en España, salvo la semana de desfase entre los dos cambios de hora (05:00).
- **Trader Lockout de MyFundedPerps.** La API no permite activarlo (solo la web: Ajustes → Trading → Trader Lockout).
  Si lo activas allí, el programa lo detecta en la primera orden rechazada, bloquea la cuenta hasta la misma hora y
  deja de intentar órdenes y cambios de stop en ella. **El bloqueo del programa no impide operar a mano en la web**:
  para eso, usa el Trader Lockout.
- **Ficha de cada cuenta**: resultado del día, barra del daily loss usado (con la marca del bloqueo automático),
  saldo, equity, margen al drawdown, operaciones del día y los bots que la usan. En el Resumen, una línea por cuenta;
  en el panel de cada bot, un bloque por cuenta con su posición real.
- **Telegram**: `/cuentas` (estado de todas) y `/bloquear C [horas|mañana]`. Los bloqueos llegan con el aviso
  «Límites, guardián y bloqueos»; las órdenes, con el aviso «Cuentas».
- `ejecuciones.csv` gana las columnas `cuenta` y `cuenta_id`. Las filas antiguas se marcan con la cuenta «Principal».

### Al actualizar

La primera vez, el programa crea la cuenta **«Principal»** con `FP_MFP_KEY` y `FP_MFP_ACCOUNT` de tu `config.env`, y el
interruptor «Enviar órdenes» toma el valor de `FP_EXEC`. Tus bots en modo sandbox siguen operando en ella sin tocar
nada (un bot sin cuentas elegidas usa la primera de la lista) y conservan su posición abierta. Desde ese momento, esas
tres líneas de `config.env` ya no se usan: todo se cambia en el panel. Las claves se guardan en `data/cuentas.json`
(solo legible por tu usuario) y el panel solo enseña sus 4 últimos caracteres.

Desde la 6.2, **con copia de seguridad** (cambia el formato del estado de los bots):

```
cd ~
tar czf kcbot2_copia_v62.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v6_3.zip -d tmp63
cp tmp63/kcbot2/{kcbot.py,nucleo.py,ejecucion.py,cuentas.py,avisos.py,panel.html,LEEME.md} ~/kcbot2/
rm -r tmp63
cd ~/kcbot2 && ./bot.sh reiniciar
./bot.sh logs        # debe salir: «Cuentas: creada «Principal» con la clave de config.env…» (Ctrl+C para salir)
```

Después abre el panel → **Cuentas**, revisa «Principal», añade las demás y elige en cada bot sus cuentas.

**Para volver a la 6.2**: `./bot.sh parar`, `cd ~ && rm -rf kcbot2 && tar xzf kcbot2_copia_v62.tgz` y
`cd kcbot2 && ./bot.sh arrancar`.

## Qué hay de nuevo en la 6.2

Nueva sección **Estadísticas** en el menú lateral, justo debajo de Resumen. Reúne las operaciones de todos tus bots
(también de los archivados) y se puede filtrar por periodo (7, 30 y 90 días, 1 año o todo), por bot, por lado
(largos o cortos) y por unidad (R o dólares, si los bots tienen «Riesgo por operación ($)»).

- **Tarjetas** con resultado, profit factor, % de acierto, esperanza por operación (y SQN), drawdown máximo con sus
  fechas, factor de recuperación, rachas y mejor y peor operación.
- **Curva de resultados y drawdown** enlazadas: al pasar el dedo o el ratón ves en las dos la fecha, el acumulado, la
  operación y la caída desde el máximo. El drawdown máximo queda marcado en la curva.
- **Calendario** de días en verde y rojo (toca un día para ver su resultado) y **tabla por meses**.
- **Mapa de día de la semana y hora de entrada**, con tu mejor y tu peor franja.
- **Distribución de resultados** en tramos de R, con la media y cuánto de lo ganado aporta tu 20 % de mejores
  operaciones.
- **Motivos de salida** (objetivo, stop, break-even, tiempo…) con su peso y su resultado.
- **Duración y resultado**: cada operación en un gráfico de dispersión, con la duración media de ganadoras y perdedoras.
- **Recorrido a favor y en contra (MFE / MAE)**: desde esta versión cada operación guarda hasta dónde llegó a favor y
  en contra antes de cerrarse. El panel te dice si tus perdedoras llegaron a +1 R antes de girarse, si tu stop
  tiene margen de sobra y qué parte del movimiento capturan tus salidas. Solo aparece en las operaciones nuevas.
- **Comparativa de bots**: sus curvas superpuestas y un ranking que se ordena tocando cada columna.

El archivo `operaciones.csv` de cada bot gana dos columnas (`mfe_r` y `mae_r`). Al arrancar, el programa lo
actualiza solo una vez y conserva todo el historial.

Actualizar desde la 6.1:

```
cd ~
unzip -o kabor_quant_v6_2.zip -d tmp62
cp tmp62/kcbot2/kcbot.py tmp62/kcbot2/nucleo.py tmp62/kcbot2/panel.html tmp62/kcbot2/LEEME.md ~/kcbot2/
rm -r tmp62
cd ~/kcbot2 && ./bot.sh reiniciar
```

## Qué hay de nuevo en la 6.1

Panel rediseñado:

- **Menú lateral con iconos**: Resumen; tus bots (con su estado y la marca SBX si operan en sandbox) y «Nuevo bot»;
  Herramientas (Estrategias, Riesgo global, Instrumentos, Avisos) y Sistema (Ajustes). Tu usuario y el botón de
  cerrar sesión están abajo. En el móvil, el menú se abre con el botón ☰ de la barra superior.
- **Secciones propias** para Riesgo global e Instrumentos (antes estaban dentro del Resumen y de la configuración de
  cada bot). La configuración del bot enlaza a Instrumentos si te falta alguno.
- **Ajustes**: cambia tu contraseña desde el panel; mira las sesiones abiertas (navegador, sistema, IP y última
  actividad) y cierra la de un dispositivo o todas las demás. Aquí están ahora los datos del sistema, el registro, el
  informe ZIP de todos los bots y el reinicio del programa.
- **Avisos y confirmaciones al estilo SweetAlert**: diálogos con icono animado para confirmar acciones (las
  peligrosas, como archivar o reiniciar, en rojo), para escribir un nombre y para confirmar que algo se ha guardado.
  Los avisos rápidos aparecen arriba a la derecha con su color y una barra de tiempo, y se cierran solos.

Actualizar desde la 6.0: copia `kcbot.py`, `acceso.py`, `panel.html` y `LEEME.md` en `~/kcbot2/` y
`./bot.sh reiniciar`. Tus bots, ajustes, usuario y sesiones no cambian.

```
cd ~
unzip -o kabor_quant_v6_1.zip -d tmp61
cp tmp61/kcbot2/kcbot.py tmp61/kcbot2/acceso.py tmp61/kcbot2/panel.html tmp61/kcbot2/LEEME.md ~/kcbot2/
rm -r tmp61
cd ~/kcbot2 && ./bot.sh reiniciar
```

## Qué hay de nuevo en la 6.0 (KaBor Quant)

El sistema pasa a llamarse **KaBor Quant** y el panel se protege como una aplicación profesional:

- **Inicio de sesión con usuario y contraseña** (`acceso.py`). La contraseña nunca se guarda: solo su huella scrypt
  (lenta a propósito, contra la fuerza bruta). La sesión va en una cookie que el JavaScript no puede leer, que no
  viaja a otras webs y que, con HTTPS, solo viaja cifrada. Las sesiones sobreviven a los reinicios del programa.
  - Sin marcar «Mantener la sesión iniciada», la sesión se cierra tras **12 horas sin actividad**; marcándola, dura
    **30 días** en ese dispositivo (`FP_SESION_HORAS`, `FP_SESION_DIAS`).
  - **5 intentos fallidos en 15 minutos** bloquean esa dirección 15 minutos, y te llega un aviso por Telegram
    (si tienes los avisos configurados). El intento queda anotado en el registro.
  - Protección contra peticiones falsificadas desde otras webs (CSRF): las órdenes al panel solo se aceptan desde el
    propio panel.
- **El token ya no va en la dirección** (`?t=…`). Era visible en el historial del navegador. `FP_WEB_TOKEN` queda
  como token de API opcional para scripts, solo por la cabecera `X-Token`.
- **HTTPS integrado.** Con `FP_WEB_HTTPS=1` el panel va cifrado con un certificado autofirmado que se crea solo
  en `data/tls/`. Quien entre por `http://` pasa automáticamente a `https://`.
- **Pantalla de acceso** con el logo de KaBor Quant en 3D animado, y **botón «Cerrar sesión»** en el panel.
- **Icono propio** en la pestaña del navegador y al añadir el panel a la pantalla de inicio del móvil.
  `kabor_quant_telegram.png` es el avatar para tu bot de Telegram: en @BotFather, `/setuserpic`, eliges tu bot y
  le mandas la imagen.
- Comandos nuevos: `./bot.sh usuario`, `./bot.sh certificado` y `./bot.sh cerrar-sesiones`. `./bot.sh comprobar`
  revisa también el acceso y el HTTPS.

### El aviso del navegador con HTTPS

El certificado lo firma tu propia Raspberry, no una autoridad, así que **la primera vez** el navegador avisará
(«La conexión no es privada»). Es normal: pulsa «Configuración avanzada» → «Acceder a …». La conexión va cifrada
igualmente. Si cambia la IP de la Raspberry, renueva el certificado con `./bot.sh certificado` y `./bot.sh reiniciar`.

**Alternativa sin avisos y desde fuera de casa: Tailscale.** Instala Tailscale en la Raspberry y en tus
dispositivos, deja `FP_WEB_HTTPS=0`, pon `FP_WEB_PROXY_HTTPS=1` y en la Raspberry:
`sudo tailscale serve --bg 8083`. Entrarás por `https://NOMBRE.TU-RED.ts.net` con candado limpio y sin abrir
ningún puerto del router. **No abras nunca el puerto del panel en el router.**

## Qué hay de nuevo en la 5.1

- **Riesgo global** (`riesgo.py`, tarjeta «Riesgo global» en el Resumen del panel). Una capa por encima de los
  límites de cada bot, que manda sobre todos a la vez:
  - **ganancia y pérdida diaria y semanal del conjunto** de bots. Al alcanzar una, ningún bot abre operaciones
    nuevas (las abiertas siguen con su stop y su objetivo). «Pausa»: vuelven solos al empezar el día o la semana
    siguiente; «Parar»: hasta que pulses «Reactivar todos los bots»;
  - **máximo de operaciones abiertas a la vez** entre todos los bots (cuenta también las entradas aprobadas que
    esperan a la apertura de la vela siguiente). Si está lleno, la señal se ignora y queda anotada en la actividad
    del bot con el motivo;
  - **qué cuenta**: «Todos los bots» (resultado del modelo, en R o en $ con el «Riesgo por operación ($)» del
    guardián de cada bot) o «Solo bots en sandbox» (resultado **real** de `ejecuciones.csv`, y solo limita a los
    bots que operan en la cuenta);
  - se guarda en `data/riesgo_global.json`, sale en `/estado` de Telegram y avisa por Telegram (aviso «Límites y
    guardián») al detener y al reanudar. Viene **desactivado**: actívalo y ponle límites desde el panel.
- Ruptura de Keltner v1.1: el calentamiento tiene en cuenta el filtro FVG (en 1m-5m con FVG de 4h ya no rechaza
  todas las señales los primeros días tras arrancar).

## Qué hay de nuevo en la 5.0

- **Ejecución en la cuenta sandbox** (`ejecucion.py`). El modelo en papel sigue decidiendo todo; un ejecutor por
  bot lo replica en la cuenta:
  - señal → orden a mercado con el **SL adjunto** (y el TP si es fijo), con el **tamaño por riesgo** (% del balance o
    importe fijo) y **tras consultar la trading policy** y el **daily loss** que queda;
  - break-even, trailing, ampliación y objetivo dinámico → se mueven el SL y el TP en la cuenta;
  - venta parcial → cierre parcial; el modelo cierra (salida de la estrategia, tiempo, cambio de ajustes) → cierre
    a mercado; la cuenta cierra sola (SL/TP) → se registra el resultado real;
  - cada operación real va a `ejecuciones.csv` con entrada del modelo y real, riesgo, P&L en $, R real y motivo.
- **Todo es configurable**: los ajustes de ejecución están en el formulario de cada bot (grupo «Ejecución en
  sandbox») y los del programa en `config.env` (tabla abajo), también como variables de entorno.
- **Estrategia nueva «Ruptura de Keltner» (`keltner_ruptura.py`)**: la que salió de las pruebas de 2019 a 2026
  con datos de MFP. Ruptura a favor del canal exterior, ADX ≤ 28, stop en la vela de ruptura y salida al volver a
  la media. Por defecto BTC 30m. En el backtest del sistema: 513 operaciones, +266R, peor drawdown 15,2R, 7 de 8
  años positivos. Acierta ~1 de cada 3 y tiene rachas de hasta 13 pérdidas: con 0,25 % de riesgo esa racha
  son ~3 %. En la cuenta entra sin TP y la salida por la media cierra la posición a mercado.
- **Estrategia «Rebote en EMA» (`ema_rebote.py` v1.1)** como módulo aparte, lista para MFP: zona horaria
  configurable, calentamiento correcto en cualquier temporalidad y validación del filtro de temporalidad mayor.
- **`./bot.sh probar ARCHIVO`**: prueba un archivo de estrategia con velas reales de MFP sin instalarlo.
- **Aviso de Telegram nuevo, «Cuenta sandbox»**: aperturas y cierres reales, señales sin orden y errores de la API.
- **En papel no cambia nada**: con los mismos ajustes, las tres estrategias dan exactamente las mismas operaciones
  que en la 4.0 (comprobado con datos reales). Si no activas la ejecución, la 5.0 se comporta como la 4.0.

## Seguridad

- Solo se puede operar en el **sandbox**: el programa rechaza claves `fp_live_` y cualquier host que no sea
  `sandbox.myfundedperpetuals.com`.
- Doble interruptor: «Enviar órdenes» en el panel → Cuentas **y** «Modo: sandbox» en cada bot (con sus cuentas). Sin
  los dos, el bot está en papel. Hasta la 6.2, el primero era `FP_EXEC=1` en `config.env`.
- Un solo bot por mercado y cuenta (compartirían la posición). Las órdenes de cada cuenta llevan su propio
  identificador (`kc<bot>-<cuenta>-<vela>-<lado>`): la misma señal en dos cuentas no se confunde nunca.
- El límite de la API (unas 300 peticiones por minuto) es por dirección IP, no por clave: más claves no dan más margen. Si hay en la cuenta una posición que no abrió el
  bot, no la toca ni abre otra en ese mercado.
- Las señales recuperadas tras un reinicio (velas antiguas) no envían órdenes.
- Las órdenes llevan identificadores fijos por señal (`Idempotency-Key`): un reintento nunca duplica una orden.
- El panel pide usuario y contraseña, frena los intentos repetidos y puede ir por HTTPS (ver la 6.0).

## Actualizar desde la 5.1 (tu caso)

Tus bots, su historial y sus ajustes no cambian. Desde el portátil (PowerShell):

```
scp "$HOME\Downloads\kabor_quant_v6.zip" usuario@raspberry.local:~/
```

En la Raspberry, copia de seguridad y actualización:

```
cd ~
tar czf kcbot2_copia_v51.tgz --exclude=venv kcbot2
unzip -o kabor_quant_v6.zip -d tmp6
cp -r tmp6/kcbot2/web ~/kcbot2/
cp tmp6/kcbot2/{kcbot.py,acceso.py,panel.html,bot.sh,LEEME.md,kabor_quant_telegram.png} ~/kcbot2/
rm -r tmp6
cd ~/kcbot2 && chmod +x bot.sh
```

Ahora, **antes de reiniciar**, crea tu usuario y activa el HTTPS:

```
./bot.sh usuario          # nombre y contraseña (mínimo 10 caracteres)
./bot.sh configurar       # deja todo igual con Intro y responde 1 a «Panel por HTTPS»
./bot.sh comprobar        # debe decir [ok] en el inicio de sesión y en HTTPS
./bot.sh reiniciar
./bot.sh panel            # te da la dirección nueva: https://…
```

La carpeta sigue llamándose `kcbot2` para que el servicio no cambie. Ya no hace falta `?t=` en la dirección:
borra el marcador antiguo y guarda el nuevo.

**Para volver a la 5.1**: `./bot.sh parar`, `cd ~ && rm -rf kcbot2/web kcbot2/acceso.py`,
`tar xzf kcbot2_copia_v51.tgz` y `cd kcbot2 && ./bot.sh arrancar`.

## Actualizar desde la 4.0

Tus bots siguen con su historial, su configuración y su operación abierta, y siguen **en papel** hasta que
actives la ejecución. Desde el portátil (PowerShell):

```
scp "$HOME\Downloads\kcbot_v5.zip" usuario@raspberry.local:~/
```

En la Raspberry, primero una copia de seguridad:

```
cd ~
tar czf kcbot2_copia_v4.tgz --exclude=venv kcbot2
```

Después, la actualización:

```
cd ~
unzip -o kcbot_v5.zip -d tmp5
cp -r tmp5/kcbot2/estrategias ~/kcbot2/
cp tmp5/kcbot2/{nucleo.py,avisos.py,kcbot.py,ejecucion.py,riesgo.py,panel.html,bot.sh,install.sh,requirements.txt,LEEME.md,ESTRATEGIAS.md} ~/kcbot2/
rm -r tmp5
cd ~/kcbot2 && chmod +x bot.sh install.sh
./bot.sh comprobar
./bot.sh reiniciar
```

`comprobar` debe mostrar `[ok]` en las cuatro estrategias. **Para volver a la 4.0**: `./bot.sh parar`,
`cd ~ && rm -rf kcbot2/data && tar xzf kcbot2_copia_v4.tgz` y `cd kcbot2 && ./bot.sh arrancar`.

## Operar en las cuentas sandbox

1. **Clave de test.** En MyFundedPerps → Ajustes → API Keys → «Create API Key», entorno **Test (Sandbox)**, nivel
   **Read And Trade**. Empieza por `fp_test_`. Si no tienes cuenta sandbox, se crea al crear la clave (100.000 $).
   Puedes crear más cuentas sandbox en el mismo sitio («Create Sandbox Account», hasta 10): la clave las ve todas.
2. **Cuentas.** Panel → **Cuentas** → «Añadir cuenta»: nombre, clave, «Buscar las cuentas de esta clave» y elige la
   cuenta. Activa «Enviar órdenes». `./bot.sh comprobar` revisa cada cuenta y su trading policy.
3. **Bot.** En el panel → pestaña del bot → Configuración → **Ejecución en sandbox**: Modo `sandbox`, marca sus
   **Cuentas**, el riesgo por operación (empieza bajo: 0,25 % o menos) y Guardar. En el panel del bot aparece la
   tarjeta «Cuentas» con la posición real de cada una; en el Resumen, la marca con el nombre de sus cuentas.
4. **Primeros días.** Activa el aviso «Cuenta sandbox» en Avisos y revisa `ejecuciones.csv` (en el informe ZIP)
   frente a `operaciones.csv`: la entrada real y la del modelo deberían diferir poco.

**El oro en el sandbox:** MyFundedPerps está retirando el mercado GOLD (`xyz:GOLD`, solo reducir posiciones); el
oro se opera en **XAU**. Para un bot de oro en sandbox, añade el instrumento XAU (Instrumentos → buscar «XAU» →
`binance|XAUUSDT`) y ponlo en el bot. Si el mercado no admite posiciones nuevas, el bot no envía la orden y lo dice.

### Cómo se comporta la ejecución

| El modelo… | En la cuenta |
|---|---|
| da una señal | Orden a mercado al cierre de la vela (≈ la apertura en la que entra el modelo). SL adjunto; TP adjunto si es RR o fijo. |
| no la puede abrir (pausa, guardián, límites) | No se envía nada. |
| abre con objetivo dinámico | El TP se coloca y se mueve vela a vela con el del modelo. |
| mueve el stop o el objetivo | `PUT exit-orders` con los niveles nuevos. |
| vende una parte (break-even) | Cierre parcial del mismo porcentaje y stop a la entrada. |
| cierra al cierre de una vela | Cierre a mercado (si «Cerrar la posición real si el modelo no la tiene» está activo). |
| — y la cuenta toca el SL/TP | Se cierra sola; se anota el resultado real aunque el modelo siga dentro. |

Antes de cada orden se comprueba: cuenta activa, trading policy sin bloqueos (trading bloqueado, exposición
restringida, restricciones, parada de plataforma, mercados de competición), el **daily loss** («No operar si tras el
SL quedaría menos de X % del daily loss», 40 % por defecto), que el precio no haya pasado ya el stop, el coste en R,
el tamaño mínimo del mercado, el nominal máximo y el apalancamiento máximo.

## Ajustes del programa (`config.env`)

Se escriben con `./bot.sh configurar` o a mano; aplica con `./bot.sh reiniciar`. Todos admiten variable de entorno.

| Clave | Por defecto | Qué es |
|---|---|---|
| `FP_WEB_PORT` / `FP_WEB_BIND` | 8083 / 0.0.0.0 | Panel: puerto e interfaz |
| `FP_WEB_HTTPS` | 0 (el asistente propone 1) | 1 = panel por HTTPS con certificado autofirmado |
| `FP_WEB_CERT` / `FP_WEB_KEY` | data/tls/cert.pem / clave.pem | Certificado propio, si prefieres usar otro |
| `FP_WEB_PROXY_HTTPS` | 0 | 1 = detrás de un proxy que ya pone HTTPS (Tailscale serve, Caddy…) |
| `FP_WEB_LOGIN` | 1 | 0 = panel sin usuario ni contraseña (no recomendado) |
| `FP_SESION_HORAS` / `FP_SESION_DIAS` | 12 / 30 | Cierre por inactividad / duración con «Mantener la sesión» |
| `FP_WEB_TOKEN` | — | Token de API opcional para scripts (cabecera `X-Token`) |
| `FP_MFP_KEY` | — | Solo la primera vez en la 6.3: crea la cuenta «Principal». Después, panel → Cuentas |
| `FP_EXEC` | 0 | Solo la primera vez en la 6.3: valor inicial de «Enviar órdenes» |
| `FP_MFP_ACCOUNT` | primera activa | Solo la primera vez en la 6.3: la cuenta de «Principal» |
| `FP_MFP_HOST` | sandbox | Solo admite el sandbox (o localhost para pruebas) |
| `FP_TEST_DAYS` | 14 | Duración prevista de la prueba (barra de progreso) |
| `FP_LOCAL_TZ` | Europe/Madrid | Hora local del panel y del filtro de horario |
| `FP_DATA_DIR` | data | Carpeta de datos |
| `FP_HIST_N` | 3000 | Velas en memoria por bot (se amplía sola si la estrategia necesita más) |
| `FP_BT_MAX` | 60000 | Velas máximas en un backtest del panel |
| `FP_MAX_BOTS` | 10 | Bots activos a la vez |
| `FP_ACCOUNT_POLL_S` | 60 | Segundos entre lecturas de cada cuenta (panel y bloqueo automático) |
| `FP_HTTP_TIMEOUT_S` | 20 | Espera máxima de cada petición a la API |
| `FP_MCP_TOKEN` | (vacío) | Token del conector con Claude (`./bot.sh conector`). Vacío = conector apagado |
| `FP_PLAN_ESPERA_MIN` | 5 | Minutos de espera para cancelar un plan activo |

Los ajustes de cada bot (estrategia, gestión, límites, guardián y ejecución) están en su formulario del panel.

## Probar una estrategia con datos reales

```
./bot.sh probar estrategias/ema_rebote.py --simbolo BTC --tf 5m --dias 30
./bot.sh probar estrategias/ema_rebote.py --simbolo XAU --tf 5m --dias 60 --ajuste rr=1.5 --ajuste sep_atr=1.5
```

Carga el archivo, lo ejecuta con velas de prueba y luego hace el backtest con velas reales del feed de MFP, con
los mismos costes y reglas que en directo. No instala nada ni crea bots.

## Activar los avisos de Telegram

1. En Telegram, abre **@BotFather**, escribe `/newbot` y sigue los pasos. Te da un token (`123456789:AAH…`).
2. En el panel → **Avisos**, pega el token y pulsa «Comprobar».
3. Abre tu bot en Telegram y escríbele `/start`. En el panel pulsa «Detectar mi chat» y «Usar este chat».
4. Activa los avisos y pulsa «Enviar mensaje de prueba». Te llegará un mensaje con un gráfico.

Debajo eliges qué avisar. Recomendado para empezar: señal, apertura, cierre, límites, problemas y resumen diario.

**Sobre la rapidez:** los bots trabajan con velas cerradas. La señal se avisa en cuanto cierra la vela que la da,
y dice a qué hora es la apertura en la que entra. La apertura se confirma al cerrar la vela de entrada (en 5m,
hasta 5 minutos después). Si quieres actuar a mano, el aviso útil es el de **señal**.

Tras un reinicio, el bot procesa las velas que se perdió. Los avisos de esas velas llegan marcados «con retraso», y
las señales antiguas no se avisan porque ya no sirven.

El token se guarda en `data/avisos.json`, solo legible por tu usuario.

## Cómo se usa el panel

**Resumen.** Totales de todos los bots y una tarjeta por bot con su estrategia. Abajo: feed en directo, la
Raspberry, si los avisos están activos, la cuenta sandbox, los bots archivados, el registro y el informe en ZIP.

**Pestaña de un bot.** «Panel» con el seguimiento y «Configuración» con todos los ajustes. El primer ajuste es la
**Estrategia**: al cambiarla, el formulario muestra los ajustes de la nueva; los comunes (mercado, límites,
guardián…) se conservan. Si vuelves a la anterior, recupera los ajustes que tenía. Guardar un cambio de
estrategia recarga el historial y cierra la operación abierta, como al cambiar de símbolo. En «Control de este bot»
puedes desactivar sus avisos.

**＋ Nuevo.** Un bot nuevo con cualquier estrategia. Desde la pestaña Estrategias, «Nuevo bot con esta» lo abre ya
con esa estrategia elegida.

**Estrategias.** Las instaladas, qué bots usa cada una, descargar el archivo, subir uno nuevo o quitarlo (solo si
ningún bot la usa).

**Un archivo de estrategia es código que se ejecuta en la Raspberry.** Sube solo archivos tuyos o que te haya
preparado yo.

## Si una estrategia falla

- Si un archivo tiene un error al cargarse, el Resumen lo avisa y esa estrategia no se puede elegir.
- Si una estrategia falla en directo, el bot marca «Error en la estrategia», registra la línea del error, lo avisa
  por Telegram y sigue con la vela siguiente.
- Si un bot usa una estrategia que ya no está, el bot queda en error hasta que la vuelvas a subir o le pongas otra.

## Comandos (en la carpeta del programa)

| Comando | Qué hace |
|---|---|
| `./bot.sh panel` | Dirección del panel |
| `./bot.sh estado` | Si el servicio está en marcha |
| `./bot.sh logs` | Registro en directo de todos los bots (Ctrl+C para salir) |
| `./bot.sh reiniciar` / `parar` / `arrancar` | Control del servicio (todos los bots) |
| `./bot.sh configurar` | Puerto, HTTPS, token de API y clave. Luego `./bot.sh reiniciar` |
| `./bot.sh usuario` | Crea el usuario del panel o le cambia la contraseña (sin reiniciar). Cambiarla cierra sus sesiones |
| `./bot.sh certificado` | Crea o renueva el certificado HTTPS. Luego `./bot.sh reiniciar` |
| `./bot.sh cerrar-sesiones` | Cierra todas las sesiones abiertas (por ejemplo, si pierdes el móvil) |
| `./bot.sh importar-bot CARPETA` | Trae un bot 2.x con su historial |
| `./bot.sh importar RUTA` | Crea un bot con la estrategia de un `config.env` 1.x |
| `./bot.sh comprobar` | Conexión, velas de cada bot, estrategias, gráficos y cuenta sandbox |
| `./bot.sh probar ARCHIVO` | Backtest de un archivo de estrategia con velas reales de MFP (`--exportar`: guarda el contexto) |
| `./bot.sh bots` | Lista los bots con el número que usan `contexto` y `analizar` (su posición en el menú) |
| `./bot.sh contexto N [--dias 365]` | Backtest con los ajustes del bot N del menú (o su nombre) y guarda el contexto de cada operación |
| `./bot.sh analizar N` | Analiza `contexto.csv` del bot N del menú o con ese nombre (o un CSV exportado): qué contexto gana y cuál pierde |
| `./bot.sh informe` | ZIP con los informes de todos los bots |
| `./bot.sh desinstalar` | Quita el servicio (los datos se conservan) |

## Dónde se guarda todo

- `estrategias/`: los archivos de estrategia (junto al programa). `estrategias/anteriores/`: versiones sustituidas.
- `data/bots.json`: la lista de bots.
- `data/bots/<nº>/`: cada bot con su `config.json`, `configs/`, `estado.json`, `operaciones.csv` (modelo),
  `ejecuciones.csv` (cuenta sandbox), `contexto.csv` (contexto de cada operación), `eventos.csv`, `velas.csv`, `informes/` y `logs/`.
- `data/cuentas.json`: las cuentas con sus claves, el interruptor «Enviar órdenes», los bloqueos y el resultado del
  día de cada cuenta. Solo legible por tu usuario. No lo compartas.
- `data/usuarios.json` (huellas de contraseña), `data/sesiones.json` (sesiones abiertas) y `data/tls/`
  (certificado HTTPS): solo legibles por tu usuario.
- `web/`: la pantalla de acceso y los iconos.
- `data/noticias.json`: ajustes del filtro de noticias. `data/cache/calendario.json`: el calendario descargado.
- `data/avisos.json`: ajustes de Telegram. `data/instrumentos.json`, `data/mercados.json` y `data/cache/`:
  compartidos por todos los bots.

El botón **Informe de todos los bots (ZIP)** del Resumen junta todo, con una carpeta por bot. Ese es el que me
mandas para analizar.

## Instalación nueva

```
cd ~
unzip kabor_quant_v6.zip
cd kcbot2
chmod +x install.sh bot.sh
./install.sh
./bot.sh usuario
```

## Notas

- El 1 de noviembre Nueva York cambia de hora: en cada bot, Configuración → Límites diarios, pon «El día empieza a
  las 00:00 UTC +» en -5.
- En la Pi 3, dibujar un gráfico para Telegram tarda un par de segundos. Se hace aparte, sin frenar a los bots.
- Con menos de 30 operaciones, un resultado del backtest dice poco.
