# HealthLog – Plan: Privacy-First Apple-Health-Analyse

> Self-hosted Analyse von Apple-Health-Daten mit Fokus auf Korrelationen,
> Anomalien und Trends — vollständig auf eigener Hardware, keine externen
> Anbieter.
>
> **Projektname:** HealthLog (Repo-Slug `healthlog`)
> **Status:** Plan-Entwurf, verfeinert. Noch nicht implementiert. Nächster
> Schritt ist Phase 0 (Daten-Audit) — erst nach Freigabe.

## 1. Grundentscheidungen

- **Datenexport:** Health Auto Export (iPhone) → REST-Automation an eigenen Endpoint
- **Topologie:** Always-On-Server (Ingestion + DB + Grafana + Routine-Analyse) ⟷ Mac (interaktive Exploration + LLM)
- **Analyse-Kern:** klassische Statistik/ML (Korrelationen, Anomalien, Trends) — **kein** LLM im kritischen Pfad
- **LLM:** Ollama auf dem Mac (32 GB Unified Memory) als **Ausbaustufe (Phase 4)** für Klartext-Reports; Zielklasse 8–14B (z. B. Qwen 2.5 14B)
- **Datenfokus:** Aktivität & Training, Schlaf & Erholung, Vitalwerte
- **Privacy:** 100 % eigene Hardware, keine externen Calls — auch das LLM bleibt lokal
- **Deployment-Ziel:** Unraid; das App-Image soll **public-fähig** sein
  (LinuxServer-Tugenden: PUID/PGID, `/config`, Env-Config) — siehe §6

## 2. Zielarchitektur

```
┌─ iPhone ──────────────────────────────────────────────┐
│  Health Auto Export  →  Automation "REST API"          │
│  (nächtlich, JSON POST, mit Secret-Header über TLS)    │
└───────────────────────────┬───────────────────────────┘
                            │  HTTPS (Reverse Proxy)
┌─ Always-On-Server (Docker Compose) ─────────────────────┐
│  healthlog  (EIN App-Container, PID 1 = s6-overlay)     │
│    ├─ service: uvicorn   → FastAPI-Ingest, 24/7         │
│    │     validiert, archiviert Roh-JSON, schreibt (Upsert)│
│    └─ service: scheduler → APScheduler-Prozess          │
│          nachts → startet Analyse als SUBPROZESS:       │
│          · Lag-Korrelationen · STL-Trends · Anomalien   │
│          → Befunde in Tabelle `findings`                │
│  TimescaleDB  (Postgres + Hypertables)  ← Single Source │
│  Grafana  → Dashboards / Trends                          │
└───────────────────────────┬───────────────────────────┘
                            │  read (psql)
┌─ Mac (Apple Silicon, 32 GB) ───────────────────────────┐
│  Interaktive Exploration: Jupyter · pandas · statsmodels│
│  (Phase 4) Ollama → Wochen-Report aus `findings`        │
└────────────────────────────────────────────────────────┘
```

### Prozess- statt Container-Trennung (Begründung)

Ingest und Analyse leben **in einem Container**, aber als **getrennte OS-Prozesse**
unter `s6-overlay` (sauberes PID 1: Signal-Handling, Zombie-Reaping, Restart-Policy).
Die rechenintensive Analyse (pandas/numpy/statsmodels) darf **nicht** im uvicorn-Prozess
laufen — sie würde über GIL/CPU-Last den Event-Loop blockieren und HAE-POSTs verzögern.
Getrennte Prozesse = OS-Level-Isolation. Die Analyse wird vom Scheduler zusätzlich als
**kurzlebiger Subprozess** (`python -m healthlog.analysis`) gestartet, sodass selbst ein
harter Crash in einer C-Extension nur diesen Subprozess killt — Scheduler **und** uvicorn
überleben, die Datenannahme ist strukturell abgeschirmt.

| Komponente | Wo | Warum |
|---|---|---|
| Ingestion (uvicorn) | App-Container, Dauerprozess | 24/7 Annahme der HAE-POSTs |
| Scheduler (APScheduler) | App-Container, eigener Prozess | triggert nachts, Logs→stdout, Env/TZ sauber |
| Analyse | App-Container, Subprozess des Schedulers | fault-isoliert, gefährdet die Annahme nicht |
| TimescaleDB | eigener Container | Postgres ohnehin separat |
| Grafana | eigener Container | fertig, direkt auf Timescale |
| Interaktive Exploration (Jupyter) | Mac | bequem lokal während der Findungsphase |
| LLM-Reports (Phase 4) | Mac | Apple Silicon (Unified Memory) spielt hier seine Stärke aus |

## 3. Tech-Stack & Begründung

| Schicht | Wahl | Warum |
|---|---|---|
| Ingestion | Eigenes **FastAPI** (statt offiziellem HAE-Server) | HAE postet JSON an jeden Endpoint; konsistent mit SQL-Storage; bekannter Stack. Der offizielle Server setzt auf MongoDB — Bruch zum SQL-Analyseziel. |
| Storage | **TimescaleDB** (Postgres-Extension) | Zeitreihen-Hypertables, Continuous Aggregates für Tageswerte, SQL für Korrelationen, native Grafana-Anbindung. |
| Scheduler | **APScheduler** (eigener Prozess unter s6) | Zeitplan im Code (versioniert), Logs→stdout (Docker-nativ), Env/TZ sauber — vs. cron-im-Container-Reibung. |
| Analyse | **Python: pandas + statsmodels + scipy + scikit-learn** | Reifer, reproduzierbarer Standard für Korrelation/Trend/Anomalie. |
| Dashboards | **Grafana** | Fertig, minimaler Aufwand, direkt auf Timescale. |
| Container-Basis | **`python:3.12-slim` + s6-overlay v3** | Schlankes Image (relevant für Public), volle Kontrolle, PUID/PGID + `/config` wie bei PocketLog. |
| LLM (Phase 4) | **Ollama**, 8–14B (z. B. Qwen 2.5 14B) | Lokal auf dem 32-GB-Mac; erhält nur fertige Befunde, nicht die Rohdaten. |

## 4. Datenmodell (Skizze)

> **Wichtig:** Das endgültige Schema wird erst **nach Phase 0** fixiert — die
> reale HAE-Payload-Struktur (Aggregation, Felder, Einheiten) entscheidet
> die Details. Die folgende Skizze nimmt die bekannten HAE-Eigenheiten vorweg.

### 4.1 Roh-Archiv (Replay-fähig)

```sql
-- Jede eingehende HAE-Payload verbatim, vor dem Parsen.
raw_ingest (id BIGSERIAL, received_at TIMESTAMPTZ DEFAULT now(),
            payload JSONB, source_ip INET, content_hash BYTEA)
            -- content_hash UNIQUE → identische Re-Posts werden verworfen.
```
Volle Fidelity: Bei Parser-/Schema-Fehlern lässt sich die Historie **neu parsen**,
ohne Datenverlust. Volumen lokal vernachlässigbar.

### 4.2 Geparste Messwerte (Hypertable)

HAE liefert die meisten Metriken **aggregiert in Buckets**, nicht roh — Herzfrequenz
typischerweise als `{Min, Avg, Max}`, Schritte/Energie als `qty` (Summe). Daher
**eine Zeile pro Metrik-Bucket** mit nullbaren Aggregat-Spalten (füllen, was HAE
liefert), **nicht** ein einzelnes `value`:

```sql
metric_samples (time TIMESTAMPTZ, metric TEXT, source TEXT, unit TEXT,
                qty   DOUBLE PRECISION,   -- Summen/Punktwerte (Steps, Energy …)
                vmin  DOUBLE PRECISION,   -- HAE "Min"
                vavg  DOUBLE PRECISION,   -- HAE "Avg"
                vmax  DOUBLE PRECISION,   -- HAE "Max"
                n     INTEGER,            -- Sample-Count im Bucket, falls vorhanden
                UNIQUE (metric, time, source))   -- Idempotenz, siehe §5
                -- Hypertable auf time; z.B. heart_rate, hrv, resting_hr,
                --   step_count, active_energy
```

### 4.3 Schlaf (eigene Tabelle, intervallbasiert)

Schlaf passt nicht in `metric_samples` — er ist ein Intervall mit Phasen:

```sql
sleep_sessions (start_time TIMESTAMPTZ, end_time TIMESTAMPTZ, source TEXT,
                sleep_date DATE,         -- ZUGEORDNET zum Aufwach-Tag (lokale TZ)
                in_bed_s INTEGER, asleep_s INTEGER,
                deep_s INTEGER, core_s INTEGER, rem_s INTEGER, awake_s INTEGER,
                UNIQUE (start_time, source))
```
**Konvention:** Eine Schlafsession wird dem **Kalendertag des Aufwachens** (lokale
TZ) zugeordnet (`sleep_date`), damit "Schlaf in der Nacht auf Tag X" mit den
Tagesmetriken von Tag X korreliert. Mitternachtsübergreifender Schlaf wird so
nicht zerschnitten.

### 4.4 Workouts

```sql
workouts (start_time, end_time, type, duration_s, energy_kcal,
          distance_m, avg_hr, max_hr, source,
          UNIQUE (start_time, type, source))
```

### 4.5 Metrik-Registry (Normalisierung)

```sql
metric_registry (metric TEXT PRIMARY KEY, display_name TEXT,
                 unit_canonical TEXT,
                 agg_default TEXT,   -- 'avg'|'sum'|'min'|'max': welcher Tageswert zählt
                 category TEXT)      -- 'activity'|'sleep'|'vital'
```
Verhindert, dass dieselbe physiologische Größe unter mehreren Namen/Einheiten
zerfasert (kcal vs. kJ, `count/min`), und sagt der Analyse, **welcher** Tagesaggregat
pro Metrik sinnvoll ist (Steps→Summe, RestingHR→Min, HRV→Avg).

**Einheiten-Wächter:** HAE liefert die Einheit pro Wert mit (`"units": "kcal"`) und kann
sie optional lokalisieren. `metric_registry.unit_canonical` ist die Soll-Einheit; beim
Ingest wird die eingehende `unit` dagegen geprüft → bei Abweichung **konvertieren**
(bekannter Faktor, z. B. kJ→kcal) **oder flaggen**, nie still übernehmen. Damit kippt
eine versehentliche Einheiten-Umstellung in HAE die Historie nicht. Ein Test pinnt das.

### 4.6 Tagesaggregate (Continuous Aggregate)

Ein einzelnes CA kann nicht "die richtige" Aggregation pro Metrik liefern, deshalb
berechnet es **alle** Aggregate pro `(day, metric)`; die Analyse pickt je Metrik die
laut `metric_registry.agg_default` passende Spalte:

```sql
daily_metrics (day, metric, avg, vmin, vmax, sum, n)
  -- time_bucket('1 day', time, 'Europe/Vienna')  ← lokale TZ, NICHT UTC!
```
**Caveat:** `avg` ist hier ein `avg(vavg)` (Mittel der Bucket-Mittel) ohne Gewichtung
— für Tagesgranularität ausreichend; bei Bedarf exakt über das Roh-Archiv nachrechenbar.

### 4.7 Befunde der Pipeline (reine Statistik, kein LLM)

```sql
findings (id, computed_at, kind TEXT,            -- 'correlation'|'anomaly'|'trend'
          metric_a TEXT, metric_b TEXT,          -- metric_b nur bei correlation
          lag_days INT, coefficient DOUBLE PRECISION,
          p_value DOUBLE PRECISION, p_value_adj DOUBLE PRECISION,  -- FDR
          ref_date DATE,                          -- Bezugstag (Anomalie/Trendpunkt)
          window_start DATE, window_end DATE,     -- Analysefenster
          severity DOUBLE PRECISION,
          details JSONB,                          -- kind-spezifische Extras
          note TEXT)
```
`ref_date`/`window_*` machen einen Befund in Grafana **markierbar** (Annotation auf
dem Tag der Anomalie). Nicht zutreffende Felder bleiben NULL.

## 5. Ingestion-Vertrag

- **Idempotenz/Upsert:** HAE sendet bei nächtlichen Exporten **überlappende Fenster**.
  Ohne Dedup wachsen Dubletten und verfälschen Tagesaggregate. Daher
  `INSERT … ON CONFLICT (UNIQUE-Key) DO UPDATE` auf alle Zieltabellen.
  `raw_ingest.content_hash` verwirft identische Re-Posts früh.
- **Backfill vs. Delta:** Beim ersten manuellen HAE-Export die **gesamte
  Apple-Health-Historie** (Jahre) einmal als Bulk-Backfill einspielen → danach
  **nächtliche Deltas**. Damit sind Korrelationen ab Tag 1 belastbar (≥6–8 Wochen).
- **Zeitzone:** Speicherung in `TIMESTAMPTZ`; alle Tages-Buckets in **lokaler TZ
  (Europe/Vienna)**, da das Tagesraster die Basis aller Analysen ist.
- **Robustheit:** Payload-Größenlimit, Secret-Header in konstanter Zeit prüfen,
  unbekannte Metriken tolerieren (landen im Roh-Archiv, werden via Registry
  nachgezogen) statt den ganzen POST abzulehnen.
- **Einheiten stabil halten:** HAE-seitig *„Use Localized Units" = OFF* und feste
  Unit Preferences pro Metrik (metrisch: kcal/km/kg/°C, HR `count/min`, HRV `ms`,
  SpO₂ `%`). Serverseitig greift zusätzlich der Einheiten-Wächter der Registry
  (§4.5) — die App-Einstellung ist Vorsorge, die Registry ist die Absicherung.

## 6. Container-Topologie & Deployment

- **Ein App-Image** `healthlog` (`python:3.12-slim` + s6-overlay v3), zwei s6-Services
  (uvicorn, APScheduler), Analyse als Subprozess (§2).
- **PUID/PGID + `/config`** wie PocketLog: Entrypoint chownt `/config`, dropt
  Privilegien. `/config` hält Persistenz, die nicht in der DB liegt: Ingest-Secret,
  Logs, evtl. DB-Backups/Export.
- **Env-getriebene Config:** `INGEST_SECRET`, `DATABASE_URL`, `TZ`,
  `ANALYSIS_CRON`/Uhrzeit, `LOG_LEVEL`, `LOG_FORMAT` (text/json) — analog PocketLog.
- **Compose:** `timescaledb` + `healthlog` + `grafana`; DB nicht öffentlich
  exponiert, Grafana hinter Auth, Reverse-Proxy/TLS vor dem Ingest.
- **Public-Tauglichkeit:** README + Beispiel-`docker-compose.yml`, sinnvolle Defaults,
  keine Telemetrie, Doku der HAE-Automation-Einrichtung.

## 7. Tests & Qualität

Übernimmt PocketLogs Philosophie: **grüne Suite ist Pflicht-Gate vor jedem Image-Push**
(§8). HealthLogs Kern-Risiko liegt nicht in CRUD, sondern in **Parser-Korrektheit**
und **Analyse-Mathematik** — genau dort liegt der Testfokus.

- **Backend-Lint:** `ruff check` + `ruff format --check` (wie PocketLog).
- **Parser-/Ingest-Tests (pytest):** echte HAE-Payload als Fixture (aus Phase 0) →
  erwartete Zeilen in `metric_samples`/`sleep_sessions`/`workouts`. Pinnt die
  Übersetzung der HAE-Eigenheiten (Min/Avg/Max-Buckets, Einheiten).
- **Idempotenz:** zweimaliges Posten derselben Payload → keine Dubletten, Upsert
  greift, `content_hash`-Dedup verwirft den Re-Post. (Das Kernrisiko aus §5.)
- **TZ-Bucketing:** Sample um Mitternacht (Europe/Vienna) landet im korrekten
  lokalen Tag; Schlafsession wird dem Aufwach-Tag zugeordnet (§4.3).
- **Analyse-Mathematik:** synthetische Reihen mit **bekannter** Lag-Korrelation →
  Pipeline findet sie beim richtigen Lag; injizierte Anomalie wird erkannt;
  FDR-Korrektur senkt Zufallstreffer. Reproduzierbar mit festem Seed.
- **Migrationen gegen echtes Postgres/TimescaleDB:** `service`-Container in CI,
  `alembic upgrade head` von leerem Schema (analog PocketLogs MariaDB-Job — die DDL
  läuft sonst nur beim Endnutzer das erste Mal).
- **Smoke:** Image bauen, mit `PUID/PGID` + `/config`-Mount + Timescale-Service
  booten, `/api/health` abfragen, prüfen dass der Entrypoint chownt und beide
  s6-Services hochkommen.
- Frontend-Tests (Vitest/Playwright) erst relevant, falls Phase 5 (eigene Web-App) kommt.

## 8. CI/CD – GitHub Workflows

Drei Workflows, gespiegelt von PocketLog, mit gepinnten Action-SHAs:

| Workflow | Trigger | Tut |
|---|---|---|
| `test.yml` | `pull_request` **+** `workflow_call` | Lint, pytest (inkl. Migrationen gegen Timescale-Service), Smoke. Reusable, damit Build-Workflows darauf gaten. |
| `dev.yml` | Push auf `dev` | `uses: test.yml` → nur bei grün: Build + Push `:dev` und `:dev-<sha>` nach **GHCR**. |
| `build.yml` | Push Tag `v*` (+ `workflow_dispatch`) | `uses: test.yml` → bei grün: Build + Push `:vX.Y.Z` und `:latest` nach **GHCR** + GitHub-Release (`generate_release_notes`). |

- **Registry: zunächst nur GHCR** (`ghcr.io/<owner>/healthlog`). Die Docker-Hub-
  `login-action`- und Mirror-`tags`-Zeilen werden **bewusst später ergänzt** — im Plan
  als TODO markiert, damit der Build sofort ohne `DOCKERHUB_*`-Secrets läuft.
- Least-Privilege: `test.yml` hat `contents: read`; nur `dev.yml`/`build.yml` fordern
  `packages: write` (bzw. `contents: write` für das Release) auf ihren eigenen Jobs an.
- Plattform vorerst `linux/amd64` (Unraid-Ziel); arm64 bei Bedarf nachrüstbar.
- **Tests blocken den Push:** ein roter Lauf verhindert `:dev`/`:vX.Y.Z` — ein kaputter
  Commit erreicht nie ein Image.

## 9. Branching & Release (CONTRIBUTING)

**Identisch zu PocketLog:**
- Entwicklung auf kurzlebigen `feature/*`-Branches, abgezweigt von `dev`.
- **PRs immer gegen `dev`**, nie direkt gegen `main`.
- `main` wird ausschließlich per PR `dev → main` aktualisiert; **Release = Tag-Push**
  `vX.Y.Z` auf `main` → triggert `build.yml` (versioniertes Image + Release).
- `:dev` = Maintainer-Staging-Kanal, `:vX.Y.Z`/`:latest` = Produktion.
- `main` und `dev` per Ruleset geschützt (PR nötig, grüne Checks, keine Force-Pushes).
- Sprache durchgängig Englisch (Code, Kommentare, Docs, Commits, PRs) — wie PocketLog.
- Ein `CONTRIBUTING.md` hält diese Regeln fest (in Phase 1 anzulegen).

## 10. Phasen-Fahrplan

### Phase 0 – Daten-Audit (zuerst, klein) ← nächster Schritt nach Freigabe
HAE-Export aktivieren, einmal manuell exportieren und die **reale Payload** prüfen:
welche Metriken, welche Aggregation (Min/Avg/Max vs. qty), welche Einheiten, wie
Schlaf strukturiert ist. **Entscheidet das endgültige Schema** (§4) und füllt die
`metric_registry`.

### Phase 1 – Ingestion + Storage
Docker-Compose mit TimescaleDB + dem `healthlog`-Image (uvicorn + Scheduler-Skelett),
Roh-Archiv + Parser + Upsert, HAE-Automation (nächtlich, Secret-Header),
Reverse-Proxy-Route. Ziel: Daten landen zuverlässig und idempotent.
**Parallel:** Repo-Grundgerüst — `CONTRIBUTING.md`, die drei Workflows (§8, GHCR-only)
und die ersten Parser-/Idempotenz-/TZ-Tests (§7), damit das Gate von Anfang an grün ist.

### Phase 2 – Exploration (Jupyter auf dem Mac)
Daten verstehen, Tagesaggregate (lokale TZ) prüfen, erste manuelle Korrelations-
und Trend-Plots. Hier lernst du, was überhaupt aussagekräftig ist.

### Phase 3 – Automatische Pipeline (im App-Container)
APScheduler triggert nachts den Analyse-Subprozess: Lag-Korrelationen (Spearman,
Lags 0–3 Tage) + Anomalie-Erkennung (28-Tage rolling Median + MAD) + Trends (STL),
Befunde → `findings` (mit FDR-`p_value_adj`). Begleitend die Analyse-Mathematik-Tests
gegen synthetische Reihen (§7).

### Phase 4 – Visualisierung + optionales LLM
Grafana-Dashboards (Trainingslast vs. HRV/Ruhepuls, Schlaf-Trends, `findings` als
Annotationen). Danach Ollama-Narration aus `findings` (Mac, 8–14B). Modell erhält
nur strukturierte Befunde; Zahlen werden gegroundet, nicht halluziniert.

### Phase 5 (optional, später)
Eigene Web-App im PocketLog-Stil.

## 11. Methodische Stolperfallen

- **Tagesraster:** Alles auf Kalendertage (lokale TZ) resamplen, sonst sind Metriken nicht vergleichbar.
- **Zeitversatz:** Effekte wirken verzögert (Training heute → HRV morgen) — Lag-Korrelationen, nicht nur Lag 0.
- **Spearman statt Pearson:** physiologische Daten sind oft nicht-linear/nicht-normalverteilt.
- **Multiple Testing:** viele Metrik-Paare → Zufallstreffer. FDR-Korrektur (`p_value_adj`), Befunde als Hinweis behandeln. Korrelation ≠ Kausalität.
- **Genug Historie:** vor ~6–8 Wochen sind Korrelationen wenig belastbar (→ Bulk-Backfill, §5).
- **Saisonalität:** Wochentag-Effekte (Wochenende ≠ Werktag) bei Anomalien berücksichtigen.
- **Aggregat-Semantik:** je Metrik den richtigen Tageswert nehmen (Registry) — Steps summieren, RestingHR minimieren, kein "avg über alles".

## 12. Privacy-Checkliste

- Ingest-Endpoint nur über TLS + Secret-Header/Token erreichbar (HAE unterstützt Custom Headers).
- DB nicht öffentlich exponiert; Grafana hinter Auth.
- LLM rein lokal (Ollama, kein API-Key, kein Netzwerk-Egress).
- Keine Telemetrie in den Komponenten aktivieren.

## 13. Offene Punkte

### Entschieden (diese Session)
- Roh-Payload wird **verbatim archiviert** (`raw_ingest`, JSONB) — Replay-fähig.
- Retention: **Rohdaten dauerhaft behalten** (Volumen winzig), CA zusätzlich; Policy später revisiten.
- Scheduler: **ein Container**, s6-overlay, uvicorn + APScheduler-Prozess, Analyse als Subprozess.
- Container-Basis: **`python:3.12-slim` + s6-overlay v3**, PUID/PGID + `/config`.
- LLM-Korridor: **8–14B** (32-GB-Mac), konkrete Wahl in Phase 4.
- CI: drei Workflows nach PocketLog-Muster; Release per Tag, Gate auf grüne Tests.
- CONTRIBUTING: `dev`-Entwicklung, PR gegen `dev`, Release per Tag auf `main`.

### TODO (bewusst aufgeschoben)
- **Docker Hub:** Workflows pushen vorerst nur nach GHCR; Docker-Hub-Login + Mirror-Tags
  (+ `DOCKERHUB_*`-Secrets) später in `dev.yml`/`build.yml` ergänzen.
- **arm64-Image** bei Bedarf (aktuell nur `linux/amd64`).

### Gated auf Phase 0 (Daten-Audit)
- Detailgranularität pro Metrik (aggregiert vs. roh) und exakte Spaltenbelegung in `metric_samples`.
- Genaue Felder/Struktur von Schlaf und Workouts laut realer HAE-Payload.
- Initiale Befüllung der `metric_registry` (Namen, Einheiten, `agg_default`).

### Gated auf Phase 4
- Konkretes Ollama-Modell + Prompt-/Grounding-Design für die Report-Narration.
