# 02 — Strategy Framework & Implementations (F3+F13)

## Strategy Loading Flow

```mermaid
flowchart TD
    A["container.py:129<br/>PluginLoader(registry).load_all(cfg.strategies)"] --> D{"for each cfg entry"}
    D -->|"has module"| E["loader.py:136<br/>load_module(module, params)"]
    D -->|"has file"| F["loader.py:138<br/>load_file(file, params)"]

    subgraph LM["load_module (loader.py:68-89)"]
        E1["split dotted path<br/>loader.py:76"]
        E2["importlib.import_module<br/>loader.py:78"]
        E3["getattr(cls_name)<br/>loader.py:82"]
        E4["cls(**params)<br/>loader.py:88"]
        E5["register(strategy)<br/>loader.py:89"]
        E1 --> E2 --> E3 --> E4 --> E5
    end

    E --> E1
    E5 --> REG["PluginRegistry.register<br/>loader.py:38-48"]
    REG --> H["SignalService(strategies=...)<br/>container.py:146"]
```

## StrategyContext Data Access

```mermaid
flowchart LR
    subgraph ctx["StrategyContext (context.py:24)"]
        NOW["now() → clock.now()<br/>context.py:39"]
        LP["last_price() → cache 'price:{sym}'<br/>context.py:45"]
        BID["bid() → cache 'bid:{sym}'<br/>context.py:49"]
        ASK["ask() → cache 'ask:{sym}'<br/>context.py:52"]
        FR["funding_rate() → cache 'funding:{sym}'<br/>context.py:55"]
        PW["prices_window() → cache 'prices_window:{sym}'<br/>context.py:59"]
        POS["position() → account<br/>context.py:72"]
        NAV["nav_usdt() → account<br/>context.py:76"]
    end

    subgraph cache_write["Cache written by SignalService BEFORE dispatch"]
        T["_on_trade → 'price:{sym}' + window append<br/>signal.py:71-78"]
        B["_on_book → 'bid:{sym}', 'ask:{sym}'<br/>signal.py:85-86"]
        F["_on_funding → 'funding:{sym}'<br/>signal.py:91"]
    end

    T -.-> LP
    T -.-> PW
    B -.-> BID
    B -.-> ASK
    F -.-> FR
```

## Signal Generation Formulas

### MomentumStrategy (momentum.py:116-130)
```
ret = price[-1] / price[0] - 1
score = clamp(ret / 0.05, -1, 1)
confidence = min(1, |ret| / 0.10)
Gate: |score| < 0.15 → no signal
```

### MeanReversionStrategy — Trade channel (mean_reversion.py:126-149)
```
z = (current - mean) / stddev
score = clamp(-z / 2.5, -1, 1)    ← negative: price > mean → short
confidence = min(1, |z| / 3.75)
Gate: |z| < 1.5 → no signal
```

### MeanReversionStrategy — Funding channel (mean_reversion.py:93-111)
```
score = clamp(-rate / 0.003, -1, 1)
confidence = min(1, |rate| / 0.003)
Gate: |rate| < 0.001 → no signal
```

### FundingRateArbStrategy (funding_rate_arb.py:64-103)
```
rate > 0 (longs pay): raw = -(rate - 0.001) / 0.004 → short
rate < 0 (shorts pay): raw = (-rate - 0.001) / 0.004 → long
score = clamp(raw, -1, 1)
confidence = min(1, |rate| / 0.0075)
Gate: |rate| < 0.001 → no signal
Decay: each TradeEvent re-emits with decay = 1 - bar/8
```

## External Dependencies
- Strategy framework (F3): `core/`, `application/events.py` (TYPE_CHECKING)
- Strategy implementations (F13): `core/` only
