# HealthLog – Plan: Privacy-First Apple-Health-Analyse

> Self-hosted Analyse von Apple-Health-Daten mit Fokus auf Korrelationen,
> Anomalien und Trends — vollständig auf eigener Hardware, keine externen
> Anbieter.
>
> **Projektname:** HealthLog (Repo-Slug `healthlog`)

## 1. Grundentscheidungen

- **Datenexport:** Health Auto Export (iPhone) → REST-Automation an eigenen Endpoint
- **Topologie:** Always-On-Server (Ingestion + DB + Grafana + Routine-Analyse) ⟷ Mac (interaktive Exploration + LLM)
- **Analyse-Kern:** klassische Statistik/ML (Korrelationen, Anomalien, Trends) — **kein** LLM im kritischen Pfad
- **LLM:** Ollama auf dem Mac als **Ausbaustufe (Phase 4)** für Klartext-Reports
- **Datenfokus:** Aktivität & Training, Schlaf & Erholung, Vitalwerte
- **Privacy:** 100 % eigene Hardware, keine externen Calls — auch das LLM bleibt lokal

## 2. Zielarchitektur

```
┌─ iPhone ──────────────────────────────────────────────┐
│  Health Auto Export  →  Automation "REST API"          │
│  (nächtlich, JSON POST, mit Secret-Header über TLS)    │
└───────────────────────────┬───────────────────────────┘
                            │  HTTPS (Reverse Proxy)
┌─ Always-On-Server (Docker Compose) ─────────────────────┐
│  health-ingest  (FastAPI, schlank)  → validiert, schreibt│
│  TimescaleDB  (Postgres + Hypertables)  ← Single Source  │
│  Routine-Analyse (Python, geplant/nächtlich)            │
│    · Lag-Korrelationen · STL-Trends · Anomalien         │
│    → Befunde in Tabelle `findings`                      │
│  Grafana  → Dashboards / Trends                          │
└───────────────────────────┬───────────────────────────┘
                            │  read (psql)
┌─ Mac (Apple Silicon) ──────────────────────────────────┐
│  Interaktive Exploration: Jupyter · pandas · statsmodels│
│  (Phase 4) Ollama → Wochen-Report aus `findings`        │
└────────────────────────────────────────────────────────┘
```

### Aufteilung der Verantwortlichkeiten

| Komponente | Wo | Warum |
|---|---|---|
| Ingestion, DB, Grafana | Server | 24/7 erreichbar |
| Automatische Stats-Pipeline | Server | leichtgewichtig + muss geplant laufen, unabhängig vom Mac |
| Interaktive Exploration (Jupyter) | Mac | bequem lokal während Findungs-/Entwicklungsphase |
| LLM-Reports (Phase 4) | Mac | Apple Silicon (Unified Memory) spielt hier seine Stärke aus |

## 3. Tech-Stack & Begründung

| Schicht | Wahl | Warum |
|---|---|---|
| Ingestion | Eigenes **FastAPI** (statt offiziellem HAE-Server) | HAE postet JSON an jeden Endpoint; konsistent mit SQL-Storage; bekannter Stack. Der offizielle Server setzt auf MongoDB — Bruch zum SQL-Analyseziel. |
| Storage | **TimescaleDB** (Postgres-Extension) | Zeitreihen-Hypertables, Continuous Aggregates für Tageswerte, SQL für Korrelationen, native Grafana-Anbindung. |
| Analyse | **Python: pandas + statsmodels + scipy + scikit-learn** | Reifer, reproduzierbarer Standard für Korrelation/Trend/Anomalie. |
| Dashboards | **Grafana** | Fertig, minimaler Aufwand, direkt auf Timescale. |
| LLM (Phase 4) | **Ollama** (z. B. Llama 3.1 8B / Qwen 2.5) | Lokal auf dem Mac; erhält nur fertige Befunde, nicht die Rohdaten. |

## 4. Datenmodell (Skizze)

```sql
-- Long-Format, eine Zeile pro Messwert (Hypertable)
metric_samples (time TIMESTAMPTZ, metric TEXT, value DOUBLE PRECISION,
                unit TEXT, source TEXT)
                -- z.B. heart_rate, hrv, resting_hr, steps, active_energy

workouts (start_time, end_time, type, duration_s, energy_kcal,
          distance_m, avg_hr, max_hr)

-- Continuous Aggregate: vorberechnete Tageswerte für die Analyse
daily_metrics (day, metric, avg/min/max/sum …)

-- Ergebnisse der Pipeline (reine Statistik, kein LLM)
findings (computed_at, kind ['correlation'|'anomaly'|'trend'],
          metric_a, metric_b, lag_days, coefficient, p_value, note)
```

## 5. Phasen-Fahrplan

### Phase 0 – Daten-Audit (zuerst, klein)
HAE-Export aktivieren, einmal manuell exportieren und sehen, welche Metriken
in welcher Auflösung wirklich ankommen (oft anders als erwartet — z. B. HRV
nur sporadisch, HR sekündlich). Entscheidet Schema-Details.

### Phase 1 – Ingestion + Storage
Docker-Compose mit TimescaleDB + FastAPI-Ingest, HAE-Automation (nächtlich,
Secret-Header), Reverse-Proxy-Route. Ziel: Daten landen zuverlässig.

### Phase 2 – Exploration (Jupyter auf dem Mac)
Daten verstehen, Tagesaggregate prüfen, erste manuelle Korrelations- und
Trend-Plots. Hier lernst du, was überhaupt aussagekräftig ist.

### Phase 3 – Automatische Pipeline (auf dem Server)
Lag-Korrelationen (Spearman, Lags 0–3 Tage) + Anomalie-Erkennung (28-Tage
rolling Median + MAD) + Trends (STL) als geplanter Job, Befunde → `findings`.

### Phase 4 – Visualisierung + optionales LLM
Grafana-Dashboards (Trainingslast vs. HRV/Ruhepuls, Schlaf-Trends, markierte
Anomalien). Danach Ollama-Narration aus `findings` (auf dem Mac).

### Phase 5 (optional, später)
Eigene Web-App im PocketLog-Stil.

## 6. Methodische Stolperfallen

- **Tagesraster:** Alles auf Kalendertage resamplen, sonst sind Metriken nicht vergleichbar.
- **Zeitversatz:** Effekte wirken verzögert (Training heute → HRV morgen) — Lag-Korrelationen, nicht nur Lag 0.
- **Spearman statt Pearson:** physiologische Daten sind oft nicht-linear/nicht-normalverteilt.
- **Multiple Testing:** viele Metrik-Paare → Zufallstreffer. Befunde als Hinweis behandeln, ggf. FDR-Korrektur. Korrelation ≠ Kausalität.
- **Genug Historie:** vor ~6–8 Wochen Daten sind Korrelationen wenig belastbar.
- **Saisonalität:** Wochentag-Effekte (Wochenende ≠ Werktag) bei Anomalien berücksichtigen.

## 7. Privacy-Checkliste

- Ingest-Endpoint nur über TLS + Secret-Header/Token erreichbar (HAE unterstützt Custom Headers).
- DB nicht öffentlich exponiert; Grafana hinter Auth.
- LLM rein lokal (Ollama, kein API-Key, kein Netzwerk-Egress).
- Keine Telemetrie in den Komponenten aktivieren.

## 8. Offene Punkte / spätere Entscheidungen

- Detailgranularität pro Metrik (aggregiert vs. roh) — nach Phase 0 festlegen.
- Scheduler für die Routine-Pipeline (cron im Container vs. APScheduler im FastAPI-Prozess).
- Retention/Downsampling-Policy in Timescale, sobald Datenvolumen wächst.
- LLM-Modellwahl & Prompt-Design für Phase 4.
