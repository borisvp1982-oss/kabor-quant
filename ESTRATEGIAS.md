# Cómo se escribe una estrategia

Una estrategia es **un archivo `.py`** en la carpeta `estrategias/`. Se instala desde el panel (pestaña
«Estrategias» → «Comprobar e instalar») o copiándolo a la carpeta y pulsando «Releer la carpeta». El programa
no se toca nunca.

Si quieres que te prepare una, pásame este documento junto con la idea de la estrategia y te devuelvo el archivo.

## Qué hace la estrategia y qué hace el sistema

La estrategia solo decide:

- **cuándo** hay señal (largo o corto),
- **dónde** va el stop,
- y, si quiere, el objetivo, una salida propia y las líneas que se dibujan en el gráfico.

El sistema hace todo lo demás, igual en directo, en el backtest del panel y en los avisos de Telegram:

- entrada en la **apertura de la vela siguiente** a la señal, con comisión y coste máximo,
- filtros comunes: dirección, espera tras cerrar, horario, tendencia de una temporalidad mayor, stop demasiado
  cerca o lejos,
- gestión: stop, objetivo, break-even y venta parcial, trailing, ampliación en velas grandes, duración máxima,
- límites diarios, guardián, estadísticas, informes y avisos.

Todos esos ajustes comunes aparecen solos en el formulario de cualquier estrategia.

## Ejemplo mínimo completo

```python
from nucleo import F, EMA, Base

ID = "cruce_emas"                 # identificador único: minúsculas, números y _
NOMBRE = "Cruce de EMAs"
VERSION = "1.0"
DESCRIPCION = "Largo cuando la EMA rápida cruza hacia arriba la lenta; corto al revés."

PARAMETROS = [
    F("ce_rapida", "EMAs", "EMA rápida", "int", 9, "", min=2, max=500),
    F("ce_lenta", "EMAs", "EMA lenta", "int", 21, "", min=3, max=1000),
    F("ce_sl_atr", "Stop", "Stop (ATR)", "float", 1.5, "", min=0.1, max=20, step=0.1),
    F("ce_rr", "Objetivo y salidas", "RR", "float", 2.0, "", min=0.1, max=20, step=0.1),
]
LINEAS = [{"key": "rapida", "nombre": "EMA rápida", "color": "gold", "estilo": "continua"},
          {"key": "lenta", "nombre": "EMA lenta", "color": "blue", "estilo": "continua"}]


def validar(P):
    return ["La EMA rápida tiene que ser menor que la lenta"] if P["ce_rapida"] >= P["ce_lenta"] else []


def calentamiento(P):
    return 4 * P["ce_lenta"]       # velas que necesitan los indicadores para ser fiables


class Estrategia(Base):
    def __init__(self, P, motor):
        super().__init__(P, motor)
        self.r, self.l = EMA(P["ce_rapida"]), EMA(P["ce_lenta"])
        self.vr = self.vl = self.prev = None

    def actualizar(self, vela):            # al principio de cada vela cerrada
        self.c = vela
        self.vr, self.vl = self.r.update(vela[4]), self.l.update(vela[4])

    def rr(self):                          # objetivo a RR fijo
        return self.P["ce_rr"]

    def senales(self):                     # al cierre de cada vela
        out, atr = [], self.m.atr
        if None not in (self.vr, self.vl, atr) and self.prev:
            cl = self.c[4]
            dif, dif_ant = self.vr - self.vl, self.prev
            if dif_ant <= 0 < dif:
                out.append({"side": 1, "sl": cl - self.P["ce_sl_atr"] * atr})
            elif dif_ant >= 0 > dif:
                out.append({"side": -1, "sl": cl + self.P["ce_sl_atr"] * atr})
        if self.vr is not None and self.vl is not None:
            self.prev = self.vr - self.vl
        return out

    def lineas(self):
        return {"rapida": self.vr, "lenta": self.vl} if self.vl is not None else {}

    def texto(self):
        if self.vl is None:
            return "Calentando las EMAs."
        return "EMA rápida {} la lenta.".format("por encima de" if self.vr > self.vl else "por debajo de")
```

El archivo completo de ejemplo es `estrategias/rsi_niveles.py`, y `estrategias/keltner_rsi.py` es la estrategia
de siempre con todas sus opciones.

## Conexión con MyFundedPerps (ejecución en sandbox)

Una estrategia **no llama nunca a la API**. El programa le da las velas cerradas del feed de MyFundedPerps y, si el
bot está en «Ejecución en sandbox → Modo: sandbox», `ejecucion.py` convierte lo que decide en órdenes:

| Lo que devuelve la estrategia | Lo que pasa en la cuenta |
|---|---|
| `{"side": 1, "sl": precio}` en `senales()` | Orden a mercado con el SL adjunto, tamaño por riesgo |
| `rr()` o `"tp"` en la señal | TP adjunto a la orden |
| `objetivo(pos)` (objetivo dinámico) | El TP se coloca y se mueve vela a vela |
| `salida(pos)` devuelve un motivo | Cierre a mercado al cierre de la vela |
| `nivel_be(pos)` (break-even «nivel de la estrategia») | Venta parcial y stop movido en la cuenta |

Para que funcione bien en la cuenta:

- **El stop siempre al otro lado del cierre de la señal.** Si no, el sistema descarta la entrada (y en la cuenta
  no se envía la orden).
- **Acota el riesgo.** Stops minúsculos dan tamaños enormes y comisiones que se comen la operación; stops muy
  lejanos, tamaños ridículos. Usa límites en ATR (como `min_riesgo`/`max_riesgo` de `ema_rebote.py`) o los comunes
  «Stop mínimo / máximo (ATR)». El filtro común «Coste máximo por operación (R)» también aplica en la cuenta.
- **Guarda tu estado** en `guardar()`/`cargar()`: tras un reinicio, el modelo y la posición real continúan juntos.
  Si el estado no se puede continuar, el modelo empieza de cero y la posición real se cierra (con «Cerrar la
  posición real si el modelo no la tiene» activo).
- **No uses la hora del sistema**, solo la de la vela (`vela[0]`, en milisegundos UTC): en directo y en el
  backtest tiene que dar lo mismo.

`estrategias/ema_rebote.py` es el ejemplo completo de una estrategia preparada así. Antes de activarla en la
cuenta, pruébala con datos reales: `./bot.sh probar estrategias/ema_rebote.py --simbolo XAU --tf 5m --dias 60`.

## Referencia

### Datos del archivo

| Nombre | Obligatorio | Qué es |
|---|---|---|
| `ID` | no (por defecto, el nombre del archivo) | Identificador único. Los bots guardan este ID. |
| `NOMBRE`, `VERSION`, `DESCRIPCION`, `AUTOR` | recomendable | Lo que se ve en el panel. |
| `PARAMETROS` | no | Ajustes propios (ver abajo). |
| `class Estrategia(Base)` | **sí** | Con al menos el método `senales()`. |
| `validar(P)` | no | Devuelve una lista de errores en texto. |
| `calentamiento(P)` | no | Velas de historial que necesita (por defecto 200). |
| `LINEAS` | no | Líneas del gráfico: `key`, `nombre`, `color` (gain, loss, gold, muted, blue, purple) y `estilo` (continua, discontinua, puntos). |
| `FILTROS` | no | Nombres de tus filtros propios: `{"codigo": "texto"}`. |
| `SALIDAS` | no | Nombres de tus motivos de salida: `{"MOTIVO": "texto"}`. |
| `AJUSTES_COMUNES` | no | Cambia un ajuste común: `{"atr_len": {"default": 10}}`. |
| `RECARGA` | no | Ajustes propios que obligan a recargar el historial al cambiarlos (casi nunca hace falta). |

### Ajustes: `F(clave, grupo, etiqueta, tipo, por_defecto, ayuda, ...)`

- Tipos: `"int"`, `"float"` (con `min`, `max`, `step`), `"bool"` y `"select"` (con `options=[...]`).
- `show_if={"otro_ajuste": valor}` lo muestra solo cuando otro ajuste tiene ese valor (o uno de una lista).
- Usa un prefijo propio en las claves (`ce_`, `rn_`…). No pueden coincidir con las comunes.
- Los grupos con el mismo nombre que uno común (`"Stop"`, `"Objetivo y salidas"`) se juntan con él.
- `COMUN("atr_len", group="Mi grupo", default=10)` dentro de `PARAMETROS` coloca un ajuste común en tu formulario.

Ajustes comunes que puedes leer en `self.P`: `symbol`, `timeframe`, `direction`, `atr_len`, `utc_off`, `risk_usd`,
y todos los de filtros, gestión y límites.

### Lo que tiene la estrategia a mano

- `self.P`: todos los ajustes.
- `self.m.atr`: ATR de la vela actual (o `None` mientras calienta).
- `self.m.prev`: vela anterior. Una vela es `(t_ms, open, high, low, close)`.
- `self.m.i`: número de vela. `self.m.pos`: operación abierta (o `None`), solo para leer.
- `self.m.hora(t)`: `(hora local, día de la semana)` con lunes = 0.
- Indicadores incrementales en `nucleo`: `EMA`, `SMA`, `RMA` (Wilder), `RSI` y `Ventana(n)` (últimos n valores).
  Todos se usan igual: `x = EMA(20)` y luego `x.update(precio)` en cada vela.

### Métodos (solo `senales()` es obligatorio)

| Método | Cuándo | Devuelve |
|---|---|---|
| `actualizar(vela)` | Al principio de cada vela cerrada | Nada. Actualiza aquí tus indicadores. |
| `senales()` | Al cierre de cada vela, siempre | Lista de señales: `{"side": 1 o -1, "sl": precio}`. Opcional: `"tp"` (objetivo fijo en precio) y `"rechazo"` (código de un filtro tuyo; la señal se cuenta como filtrada). |
| `rr()` | Al abrir | Múltiplo del riesgo para el objetivo, o `None`. |
| `objetivo(pos)` | En cada vela si no hay RR ni `tp` | Objetivo dinámico en precio, o `None`. |
| `salida(pos)` | Al cierre de cada vela con operación | Un motivo corto para cerrar al cierre, o `None`. |
| `nivel_be(pos)` | Si el disparador del break-even no es «R» | Precio que dispara el break-even. |
| `lineas()` | Cada vela | `{clave: precio}` de las `LINEAS`. |
| `texto()`, `indicadores()` | Panel y avisos | Una frase de estado y un texto corto («RSI 42»). |
| `guardar()`, `cargar(s)` | Reinicios | Estado propio a conservar (JSON). |
| `reiniciar()` | Fin del calentamiento | Olvidar preparaciones en curso. |

### Reglas que conviene respetar

- **Nunca mires el futuro.** Usa solo la vela actual y las anteriores.
- La señal se da al cierre y la entrada es en la apertura de la vela siguiente: el stop de la señal tiene que
  quedar al otro lado de esa apertura o la entrada se descarta.
- Si una vela toca el stop y el objetivo, el sistema asume primero el stop.
- Si `senales()` falla en directo, el bot registra el error, lo avisa por Telegram y sigue con la vela siguiente.
