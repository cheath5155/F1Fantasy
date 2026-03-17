"""
f1fantasy.org — Python Backend
================================
Uses FastF1 to pull race data and calculate fantasy points.
Exposes a Flask REST API consumed by the frontend HTML.

Install dependencies:
    pip install fastf1 flask flask-cors sqlalchemy

Run:
    python backend.py

The API will be live at http://localhost:5000
"""

import os
import json
import logging
from datetime import datetime, timezone
import math
import fastf1
import pandas as pd
from flask import Flask, jsonify, request
from flask_cors import CORS
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean,
    ForeignKey, UniqueConstraint, text
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

SEASON = 2026
CACHE_DIR = "./fastf1_cache"          # FastF1 local cache folder
DB_PATH   = "sqlite:///f1fantasy.db"  # SQLite database file

# Enable FastF1 cache so you don't re-download data every run
os.makedirs(CACHE_DIR, exist_ok=True)
fastf1.Cache.enable_cache(CACHE_DIR)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# LEAGUE ROSTER
# driver_last_name must match FastF1's Driver column exactly
# ─────────────────────────────────────────────

FANTASY_TEAMS = {
    "Julia":   ["Hamilton", "Albon",    "Bottas",   "Bortoleto"],
    "Sophie":  ["Russell",  "Hadjar",   "Bearman",  "Hülkenberg"],
    "Jackson": ["Verstappen","Sainz",   "Alonso",   "Pérez"],
    "Connor":  ["Antonelli","Piastri",  "Ocon",     "Gasly"],
    "Gracie":  ["Norris",   "Leclerc",  "Lawson",   "Lindblad"],
}

# Reverse lookup: driver surname -> fantasy manager
DRIVER_TO_MANAGER = {
    driver: manager
    for manager, drivers in FANTASY_TEAMS.items()
    for driver in drivers
}

# ─────────────────────────────────────────────
# POINTS SYSTEM (matching Rules & Points page)
# ─────────────────────────────────────────────

RACE_POINTS = {
    1: 30, 2: 24, 3: 20, 4: 16, 5: 13,
    6: 10, 7:  8, 8:  6, 9:  4, 10: 2,
}
# Positions 11-15 earn 1 pt, 16+ earn 0

QUALI_POINTS = {
    1: 10, 2: 8, 3: 6, 4: 5, 5: 4,
}
# P6-P10 earn 3 pts, P11-P15 earn 1, P16+ earn 0

FASTEST_LAP_BONUS    = 5
BEAT_TEAMMATE_BONUS  = 3
MAX_POSITION_PENALTY = -5   # floor for positions-lost penalty


def race_finish_points(position: int) -> int:
    """Points for finishing position in the race."""
    if position <= 10:
        return RACE_POINTS.get(position, 0)
    elif position <= 15:
        return 1
    return 0


def quali_position_points(position: int) -> int:
    """Points for qualifying position."""
    if position <= 5:
        return QUALI_POINTS.get(position, 0)
    elif position <= 10:
        return 3
    elif position <= 15:
        return 1
    return 0


def position_change_points(grid_pos: int, finish_pos: int) -> int:
    """
    +1 per position gained, -1 per position lost.
    Penalty floored at MAX_POSITION_PENALTY.
    """
    if grid_pos is None or finish_pos is None:
        return 0
    delta = grid_pos - finish_pos          # positive = gained positions
    if delta < 0:
        return max(delta, MAX_POSITION_PENALTY)
    return delta


# ─────────────────────────────────────────────
# DATABASE MODELS
# ─────────────────────────────────────────────

Base = declarative_base()


class Manager(Base):
    __tablename__ = "managers"
    id       = Column(Integer, primary_key=True)
    name     = Column(String, unique=True, nullable=False)
    initials = Column(String(2), nullable=False)
    points   = relationship("ManagerRacePoints", back_populates="manager")


class Driver(Base):
    __tablename__ = "drivers"
    id           = Column(Integer, primary_key=True)
    number       = Column(Integer)
    surname      = Column(String, unique=True, nullable=False)
    full_name    = Column(String)
    f1_team      = Column(String)
    fantasy_manager = Column(String, nullable=True)   # None = undrafted
    color_class  = Column(String)
    race_points  = relationship("DriverRacePoints", back_populates="driver")


class Race(Base):
    __tablename__ = "races"
    id         = Column(Integer, primary_key=True)
    season     = Column(Integer, nullable=False)
    round_num  = Column(Integer, nullable=False)
    name       = Column(String)
    date       = Column(String)          # ISO string
    processed  = Column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("season", "round_num"),)


class DriverRacePoints(Base):
    """Points a single driver scored in a single race."""
    __tablename__ = "driver_race_points"
    id              = Column(Integer, primary_key=True)
    driver_id       = Column(Integer, ForeignKey("drivers.id"))
    race_id         = Column(Integer, ForeignKey("races.id"))
    quali_pts       = Column(Integer, default=0)
    race_finish_pts = Column(Integer, default=0)
    pos_change_pts  = Column(Integer, default=0)
    fastest_lap_pts = Column(Integer, default=0)
    beat_teammate_pts = Column(Integer, default=0)
    total_pts       = Column(Integer, default=0)
    finish_pos      = Column(Integer, nullable=True)
    grid_pos        = Column(Integer, nullable=True)
    driver          = relationship("Driver", back_populates="race_points")
    __table_args__ = (UniqueConstraint("driver_id", "race_id"),)


class ManagerRacePoints(Base):
    """Aggregate points a fantasy manager scored in a single race."""
    __tablename__ = "manager_race_points"
    id         = Column(Integer, primary_key=True)
    manager_id = Column(Integer, ForeignKey("managers.id"))
    race_id    = Column(Integer, ForeignKey("races.id"))
    total_pts  = Column(Integer, default=0)
    manager    = relationship("Manager", back_populates="points")
    __table_args__ = (UniqueConstraint("manager_id", "race_id"),)


# ─────────────────────────────────────────────
# DATABASE SETUP
# ─────────────────────────────────────────────

engine  = create_engine(DB_PATH, echo=False)
Session = sessionmaker(bind=engine)


def init_db():
    """Create tables and seed managers + drivers if empty."""
    Base.metadata.create_all(engine)
    session = Session()

    # Seed managers
    if session.query(Manager).count() == 0:
        manager_initials = {
            "Julia": "JU", "Sophie": "SO", "Jackson": "JA",
            "Connor": "CO", "Gracie": "GR",
        }
        for name, initials in manager_initials.items():
            session.add(Manager(name=name, initials=initials))
        log.info("Seeded managers.")

    # Seed drivers
    if session.query(Driver).count() == 0:
        driver_seed = [
            (44, "Hamilton",   "Lewis Hamilton",    "Ferrari",       "Julia",   "c-ferrari"),
            (23, "Albon",      "Alexander Albon",   "Williams",      "Julia",   "c-williams"),
            (77, "Bottas",     "Valtteri Bottas",   "Cadillac",      "Julia",   "c-cadillac"),
            (5,  "Bortoleto",  "Gabriel Bortoleto", "Audi",          "Julia",   "c-audi"),
            (63, "Russell",    "George Russell",    "Mercedes",      "Sophie",  "c-mercedes"),
            (6,  "Hadjar",     "Isack Hadjar",      "Red Bull",      "Sophie",  "c-redbull"),
            (87, "Bearman",    "Oliver Bearman",    "Haas",          "Sophie",  "c-haas"),
            (27, "Hülkenberg", "Nico Hülkenberg",   "Audi",          "Sophie",  "c-audi"),
            (3,  "Verstappen", "Max Verstappen",    "Red Bull",      "Jackson", "c-redbull"),
            (55, "Sainz",      "Carlos Sainz",      "Williams",      "Jackson", "c-williams"),
            (14, "Alonso",     "Fernando Alonso",   "Aston Martin",  "Jackson", "c-astonmartin"),
            (11, "Pérez",      "Sergio Pérez",      "Cadillac",      "Jackson", "c-cadillac"),
            (12, "Antonelli",  "Kimi Antonelli",    "Mercedes",      "Connor",  "c-mercedes"),
            (81, "Piastri",    "Oscar Piastri",     "McLaren",       "Connor",  "c-mclaren"),
            (31, "Ocon",       "Esteban Ocon",      "Haas",          "Connor",  "c-haas"),
            (10, "Gasly",      "Pierre Gasly",      "Alpine",        "Connor",  "c-alpine"),
            (4,  "Norris",     "Lando Norris",      "McLaren",       "Gracie",  "c-mclaren"),
            (16, "Leclerc",    "Charles Leclerc",   "Ferrari",       "Gracie",  "c-ferrari"),
            (30, "Lawson",     "Liam Lawson",       "Racing Bulls",  "Gracie",  "c-racingbulls"),
            (41, "Lindblad",   "Arvid Lindblad",    "Racing Bulls",  "Gracie",  "c-racingbulls"),
            (43, "Colapinto",  "Franco Colapinto",  "Alpine",        None,      "c-alpine"),
            (18, "Stroll",     "Lance Stroll",      "Aston Martin",  None,      "c-astonmartin"),
        ]
        for num, surname, full, f1team, manager, color in driver_seed:
            session.add(Driver(
                number=num, surname=surname, full_name=full,
                f1_team=f1team, fantasy_manager=manager, color_class=color
            ))
        log.info("Seeded drivers.")

    session.commit()
    session.close()


# ─────────────────────────────────────────────
# FASTF1 RACE PROCESSING
# ─────────────────────────────────────────────

def get_fastest_lap_driver(session_data) -> str | None:
    """Return the surname of the driver with the fastest lap, or None."""
    try:
        laps = session_data.laps.pick_fastest()
        if laps is not None:
            return laps["LastName"] if "LastName" in laps else None
    except Exception:
        pass
    return None




def process_race(round_num: int, force: bool = False) -> dict:
    """
    Pull data for a race weekend via FastF1 and calculate fantasy points.
    Stores results in the database.

    Args:
        round_num: The F1 round number (1-24)
        force:     Re-process even if already stored

    Returns:
        Summary dict of what was processed
    """
    session = Session()

    try:
        # Check if already processed
        race_row = session.query(Race).filter_by(
            season=SEASON, round_num=round_num
        ).first()

        if race_row and race_row.processed and not force:
            log.info(f"Round {round_num} already processed. Use force=True to reprocess.")
            return {"status": "already_processed", "round": round_num}

        # ── Load data via FastF1 ──────────────────────────────────
        log.info(f"Loading FastF1 data for {SEASON} Round {round_num}…")

        quali_session = fastf1.get_session(SEASON, round_num, "Q")
        race_session  = fastf1.get_session(SEASON, round_num, "R")

        quali_session.load(telemetry=False, weather=False, messages=False)
        race_session.load(telemetry=False, weather=False, messages=False)

        event_name = race_session.event["EventName"]
        event_date = str(race_session.event["EventDate"])

        log.info(f"Loaded: {event_name} ({event_date})")

        # ── Helper: safely convert a value to int ────────────────
        def safe_int(val):
            try:
                f = float(val)
                if pd.isna(f) or not math.isfinite(f):
                    return None
                return int(f)
            except (TypeError, ValueError):
                return None

        # ── Qualifying results ────────────────────────────────────
        quali_results = quali_session.results[["DriverNumber", "LastName", "Position"]].copy()
        quali_map = {}
        for _, qrow in quali_results.iterrows():
            pos = safe_int(qrow["Position"])
            if pos is not None:
                quali_map[qrow["LastName"]] = pos

        # ── Race results ──────────────────────────────────────────
        race_results = race_session.results[[
            "DriverNumber", "LastName", "Position", "GridPosition", "Status"
        ]].copy()
        race_results["Position"]     = pd.to_numeric(race_results["Position"],     errors="coerce")
        race_results["GridPosition"] = pd.to_numeric(race_results["GridPosition"], errors="coerce")

        # ── Fastest lap ───────────────────────────────────────────
        try:
            fastest_laps = race_session.laps.pick_fastest()
            fl_driver    = fastest_laps["LastName"] if fastest_laps is not None else None
        except Exception:
            fl_driver = None

        # ── Teammate comparison (race finish) ─────────────────────
        # Map surname -> finish position for beat-teammate logic
        finish_pos_map = {}
        for _, rrow in race_results.iterrows():
            pos = safe_int(rrow["Position"])
            if pos is not None:
                finish_pos_map[rrow["LastName"]] = pos

        # F1 team -> list of driver surnames on that team
        f1_team_drivers: dict[str, list[str]] = {}
        db_drivers = session.query(Driver).all()
        for d in db_drivers:
            f1_team_drivers.setdefault(d.f1_team, []).append(d.surname)

        # ── Ensure Race row exists ────────────────────────────────
        if not race_row:
            race_row = Race(season=SEASON, round_num=round_num,
                            name=event_name, date=event_date)
            session.add(race_row)
            session.flush()

        # ── Calculate points per driver ───────────────────────────
        summary = {}

        for _, row in race_results.iterrows():
            surname    = row["LastName"]
            finish_pos = safe_int(row["Position"])
            grid_pos   = safe_int(row["GridPosition"])

            db_driver = session.query(Driver).filter_by(surname=surname).first()
            if db_driver is None:
                log.warning(f"Driver not found in DB: {surname} — skipping")
                continue

            # Qualifying points
            q_pos = quali_map.get(surname)
            q_pts = quali_position_points(q_pos) if q_pos is not None else 0

            # Race finish points
            rf_pts = race_finish_points(finish_pos) if finish_pos else 0

            # Position change points
            pc_pts = position_change_points(grid_pos, finish_pos)

            # Fastest lap bonus
            fl_pts = FASTEST_LAP_BONUS if surname == fl_driver else 0

            # Beat teammate bonus
            bt_pts = 0
            teammates = [
                s for s in f1_team_drivers.get(db_driver.f1_team, [])
                if s != surname
            ]
            for tm_surname in teammates:
                tm_pos = finish_pos_map.get(tm_surname)
                if finish_pos and tm_pos and finish_pos < tm_pos:
                    bt_pts = BEAT_TEAMMATE_BONUS
                    break

            total = q_pts + rf_pts + pc_pts + fl_pts + bt_pts

            # Upsert DriverRacePoints row
            drp = session.query(DriverRacePoints).filter_by(
                driver_id=db_driver.id, race_id=race_row.id
            ).first()

            if drp:
                session.delete(drp)
                session.flush()

            session.add(DriverRacePoints(
                driver_id=db_driver.id,
                race_id=race_row.id,
                quali_pts=q_pts,
                race_finish_pts=rf_pts,
                pos_change_pts=pc_pts,
                fastest_lap_pts=fl_pts,
                beat_teammate_pts=bt_pts,
                total_pts=total,
                finish_pos=finish_pos,
                grid_pos=grid_pos,
            ))

            summary[surname] = {
                "quali": q_pts, "race": rf_pts, "pos_change": pc_pts,
                "fastest_lap": fl_pts,
                "beat_teammate": bt_pts, "total": total,
            }

            log.info(f"  {surname:<14} Q:{q_pts:3}  R:{rf_pts:3}  PC:{pc_pts:+3}  "
                     f"FL:{fl_pts}  BT:{bt_pts}  → {total} pts")

        # ── Aggregate manager points for this race ────────────────
        managers = session.query(Manager).all()
        for mgr in managers:
            mgr_total = 0
            for driver_surname in FANTASY_TEAMS.get(mgr.name, []):
                mgr_total += summary.get(driver_surname, {}).get("total", 0)

            mrp = session.query(ManagerRacePoints).filter_by(
                manager_id=mgr.id, race_id=race_row.id
            ).first()
            if mrp:
                session.delete(mrp)
                session.flush()

            session.add(ManagerRacePoints(
                manager_id=mgr.id,
                race_id=race_row.id,
                total_pts=mgr_total,
            ))
            log.info(f"  Manager {mgr.name}: {mgr_total} pts this race")

        race_row.processed = True
        session.commit()

        log.info(f"✓ Round {round_num} ({event_name}) fully processed.")
        return {"status": "ok", "round": round_num, "name": event_name, "summary": summary}

    except Exception as e:
        session.rollback()
        log.error(f"Error processing round {round_num}: {e}", exc_info=True)
        return {"status": "error", "round": round_num, "error": str(e)}

    finally:
        session.close()


def process_all_completed_races():
    """
    Loop through all rounds of the season and process any that
    have finished but haven't been stored yet.
    """
    schedule = fastf1.get_event_schedule(SEASON, include_testing=False)
    now = datetime.now(timezone.utc)

    for _, event in schedule.iterrows():
        round_num  = int(event["RoundNumber"])
        event_date = event["EventDate"]

        # Skip if race hasn't happened yet (give a 4-hour buffer after race start)
        if pd.Timestamp(event_date, tz="UTC") > now:
            log.info(f"Round {round_num} ({event['EventName']}) not yet — skipping.")
            continue

        result = process_race(round_num)
        if result["status"] == "already_processed":
            log.info(f"Round {round_num} already done.")


# ─────────────────────────────────────────────
# FLASK API
# ─────────────────────────────────────────────

app = Flask(__name__)
CORS(app)


@app.route("/api/standings")
def api_standings():
    """
    Returns all managers ranked by total season points.
    
    Response:
    [
      {
        "rank": 1,
        "name": "Julia",
        "initials": "JU",
        "total_points": 342,
        "last_race_points": 48,
        "drivers": ["Hamilton", "Albon", "Bottas", "Bortoleto"]
      }, ...
    ]
    """
    session = Session()
    try:
        managers = session.query(Manager).all()

        # Get last completed race
        last_race = session.query(Race).filter_by(
            season=SEASON, processed=True
        ).order_by(Race.round_num.desc()).first()

        results = []
        for mgr in managers:
            # Total season points
            total = session.execute(
                text("""
                    SELECT COALESCE(SUM(mrp.total_pts), 0)
                    FROM manager_race_points mrp
                    JOIN races r ON mrp.race_id = r.id
                    WHERE mrp.manager_id = :mid AND r.season = :season AND r.processed = 1
                """),
                {"mid": mgr.id, "season": SEASON}
            ).scalar()

            # Last race points
            last_race_pts = 0
            if last_race:
                mrp = session.query(ManagerRacePoints).filter_by(
                    manager_id=mgr.id, race_id=last_race.id
                ).first()
                if mrp:
                    last_race_pts = mrp.total_pts

            results.append({
                "name": mgr.name,
                "initials": mgr.initials,
                "total_points": int(total),
                "last_race_points": last_race_pts,
                "drivers": FANTASY_TEAMS.get(mgr.name, []),
            })

        # Sort by total points descending, add rank
        results.sort(key=lambda x: x["total_points"], reverse=True)
        for i, r in enumerate(results):
            r["rank"] = i + 1

        return jsonify(results)

    finally:
        session.close()


@app.route("/api/drivers")
def api_drivers():
    """
    Returns all drivers with their season and last-race points.

    Response:
    [
      {
        "number": 4,
        "surname": "Norris",
        "full_name": "Lando Norris",
        "f1_team": "McLaren",
        "fantasy_manager": "Gracie",
        "color_class": "c-mclaren",
        "season_points": 180,
        "last_race_points": 35
      }, ...
    ]
    """
    session = Session()
    try:
        drivers = session.query(Driver).all()

        last_race = session.query(Race).filter_by(
            season=SEASON, processed=True
        ).order_by(Race.round_num.desc()).first()

        results = []
        for d in drivers:
            total = session.execute(
                text("""
                    SELECT COALESCE(SUM(drp.total_pts), 0)
                    FROM driver_race_points drp
                    JOIN races r ON drp.race_id = r.id
                    WHERE drp.driver_id = :did AND r.season = :season AND r.processed = 1
                """),
                {"did": d.id, "season": SEASON}
            ).scalar()

            last_race_pts = 0
            if last_race:
                drp = session.query(DriverRacePoints).filter_by(
                    driver_id=d.id, race_id=last_race.id
                ).first()
                if drp:
                    last_race_pts = drp.total_pts

            results.append({
                "number": d.number,
                "surname": d.surname,
                "full_name": d.full_name,
                "f1_team": d.f1_team,
                "fantasy_manager": d.fantasy_manager,
                "color_class": d.color_class,
                "season_points": int(total),
                "last_race_points": last_race_pts,
            })

        return jsonify(results)

    finally:
        session.close()


@app.route("/api/lineup/<manager_name>")
def api_lineup(manager_name: str):
    """
    Returns a manager's 4 drivers with per-driver season and last-race points.

    Example: GET /api/lineup/Julia
    """
    session = Session()
    try:
        manager = session.query(Manager).filter_by(name=manager_name).first()
        if not manager:
            return jsonify({"error": "Manager not found"}), 404

        last_race = session.query(Race).filter_by(
            season=SEASON, processed=True
        ).order_by(Race.round_num.desc()).first()

        driver_surnames = FANTASY_TEAMS.get(manager_name, [])
        drivers_out = []

        season_total = 0
        last_race_total = 0

        for surname in driver_surnames:
            d = session.query(Driver).filter_by(surname=surname).first()
            if not d:
                continue

            total = session.execute(
                text("""
                    SELECT COALESCE(SUM(drp.total_pts), 0)
                    FROM driver_race_points drp
                    JOIN races r ON drp.race_id = r.id
                    WHERE drp.driver_id = :did AND r.season = :season AND r.processed = 1
                """),
                {"did": d.id, "season": SEASON}
            ).scalar()

            last_race_pts = 0
            if last_race:
                drp = session.query(DriverRacePoints).filter_by(
                    driver_id=d.id, race_id=last_race.id
                ).first()
                if drp:
                    last_race_pts = drp.total_pts

            season_total    += int(total)
            last_race_total += last_race_pts

            drivers_out.append({
                "number": d.number,
                "full_name": d.full_name,
                "f1_team": d.f1_team,
                "color_class": d.color_class,
                "season_points": int(total),
                "last_race_points": last_race_pts,
            })

        return jsonify({
            "manager": manager_name,
            "initials": manager.initials,
            "season_total": season_total,
            "last_race_total": last_race_total,
            "drivers": drivers_out,
        })

    finally:
        session.close()


@app.route("/api/races")
def api_races():
    """
    Returns the full race schedule with processing status.

    Response:
    [
      {
        "round": 1,
        "name": "Australian Grand Prix",
        "date": "2026-03-07",
        "processed": true
      }, ...
    ]
    """
    session = Session()
    try:
        races = session.query(Race).filter_by(season=SEASON).order_by(Race.round_num).all()
        return jsonify([
            {
                "round": r.round_num,
                "name": r.name,
                "date": r.date,
                "processed": r.processed,
            }
            for r in races
        ])
    finally:
        session.close()


@app.route("/api/race/<int:round_num>")
def api_race_detail(round_num: int):
    """
    Detailed breakdown for a single race — all drivers' points components.

    Example: GET /api/race/1
    """
    session = Session()
    try:
        race = session.query(Race).filter_by(season=SEASON, round_num=round_num).first()
        if not race or not race.processed:
            return jsonify({"error": "Race not found or not yet processed"}), 404

        drps = session.query(DriverRacePoints).filter_by(race_id=race.id).all()
        rows = []
        for drp in drps:
            d = drp.driver
            rows.append({
                "surname": d.surname,
                "full_name": d.full_name,
                "fantasy_manager": d.fantasy_manager,
                "f1_team": d.f1_team,
                "grid_pos": drp.grid_pos,
                "finish_pos": drp.finish_pos,
                "quali_pts": drp.quali_pts,
                "race_finish_pts": drp.race_finish_pts,
                "pos_change_pts": drp.pos_change_pts,
                "fastest_lap_pts": drp.fastest_lap_pts,
                "beat_teammate_pts": drp.beat_teammate_pts,
                "total_pts": drp.total_pts,
            })

        rows.sort(key=lambda x: x["total_pts"], reverse=True)

        return jsonify({
            "round": round_num,
            "name": race.name,
            "date": race.date,
            "results": rows,
        })

    finally:
        session.close()


@app.route("/api/update/<int:round_num>", methods=["POST"])
def api_update_race(round_num: int):
    """
    Trigger a race data pull and points calculation for a given round.
    Call this after each race weekend:

        curl -X POST http://localhost:5000/api/update/1

    Optional query param: ?force=true to reprocess an already-stored race.
    """
    force  = request.args.get("force", "false").lower() == "true"
    result = process_race(round_num, force=force)
    return jsonify(result)


@app.route("/api/update/all", methods=["POST"])
def api_update_all():
    """
    Process all completed races that haven't been stored yet.

        curl -X POST http://localhost:5000/api/update/all
    """
    process_all_completed_races()
    return jsonify({"status": "ok", "message": "All completed races processed."})


# ─────────────────────────────────────────────
# STARTUP — runs whether launched via gunicorn or python directly
# ─────────────────────────────────────────────
init_db()
log.info("Database ready.")

if __name__ == "__main__":
    log.info("Starting F1 Fantasy API on http://localhost:5000")
    app.run(debug=True, host="0.0.0.0", port=5000)
