# Importing Real Road Corridors from OpenStreetMap (OSM) into SUMO

This guide explains how to use SUMO's `osmWebWizard` tool to import any real-world arterial corridor or intersection into SUMO, extract the network and traffic demands, and plug it into LifeLane.

---

## 1. What is `osmWebWizard`?

`osmWebWizard.py` is an official visual utility bundled with SUMO. It opens an interactive OpenStreetMap in your browser, allows you to select a bounding box anywhere in the world (e.g. an emergency corridor between a hospital and an arterial road), selects transportation modes, generates realistic background traffic flows, and automatically launches the simulation in `sumo-gui`.

---

## 2. Prerequisites

Ensure SUMO is installed on your system:
- **Windows**: Download the official installer from [https://eclipse.dev/sumo/](https://eclipse.dev/sumo/).
- Ensure the `SUMO_HOME` environment variable is set (e.g. `C:\Program Files (x86)\Eclipse\Sumo`).
- Ensure Python is installed with dependencies:
  ```bash
  pip install traci sumolib
  ```

---

## 3. Step-by-Step Corridor Import

### Step 1: Launch osmWebWizard
In your terminal / PowerShell:
```bash
# If SUMO is on your PATH:
osmWebWizard

# Or invoke directly via python:
python "%SUMO_HOME%\tools\osmWebWizard.py"
```
*(On Linux/macOS: `python3 $SUMO_HOME/tools/osmWebWizard.py`)*

### Step 2: Select Area in Browser
1. An interactive map will open at `http://localhost:8010`.
2. Navigate to your desired city and zoom in on your target corridor or intersection.
3. Check **"Select Area"** in the top-left toolbar and drag the blue bounding rectangle around the corridor.

### Step 3: Configure Modes and Demand
On the left control panel:
- **Road Types**: Enable *Roads* (cars), *Public Transport* (buses), and *Motorcycles*.
- **Traffic Generation**:
  - `Cars`: Set probability (e.g., `10 - 20` for high traffic corridors).
  - `Buses`: Set regular transit intervals (e.g., `2 - 5`).
  - `Trucks` / `Pedestrians`: Enable or disable as needed for your scenario.

### Step 4: Generate Scenario
Click **"Generate Scenario"** (green button).

SUMO will automatically:
1. Download OSM data (`.osm.xml`).
2. Run `netconvert` with standard OSM typemaps to build `osm.net.xml.gz` / `osm.net.xml`.
3. Detect existing traffic lights and junctions from OSM tags (`highway=traffic_signals`).
4. Generate routes using `randomTrips.py` into `osm.passenger.trips.xml` etc.
5. Create `osm.sumocfg` in a generated timestamped directory inside your user home folder or working directory.
6. Automatically launch `sumo-gui` with the scenario running.

---

## 4. Extracting the Corridor for LifeLane

Once `osmWebWizard` generates the files, you can copy them into your project:

1. Locate the generated scenario folder (indicated in the terminal, usually under `~/SUMO/` or `C:\Users\<user>\SUMO\YYYY-MM-DD-hh-mm-ss\`).
2. Files to copy into your project folder (e.g., `lifelane/corridor_osm/`):
   - `osm.net.xml` (or unpack `osm.net.xml.gz`)
   - `osm.sumocfg`
   - `osm.passenger.rou.xml` (and any bus/transit route files)
   - `osm.view.xml`

---

## 5. Controlling the Imported Corridor with LifeLane

To run adaptive control or emergency vehicle preemption on the imported OSM network:

### 1. Identify Junction IDs
Open the network in `netedit` or `sumo-gui`:
```bash
netedit osm.net.xml
```
- Click on any traffic light junction to see its ID (e.g., `cluster_12345678` or `node_987654`).
- Inspect the signal phases and incoming lane IDs.

### 2. Inject Emergency Vehicles
You can add an ambulance or fire truck to `osm.passenger.rou.xml`:
```xml
<vType id="ambulance" vClass="emergency" guiShape="emergency" color="1,0,0"
       speedFactor="1.4" impatience="1.0">
    <param key="has.bluelight.device" value="true"/>
</vType>

<vehicle id="emergency_01" type="ambulance" route="your_corridor_route" depart="120"/>
```

### 3. Connect TraCI
Point `run_baseline.py` (or LifeLane's controller) to `osm.sumocfg`:
```bash
python run_baseline.py --config corridor_osm/osm.sumocfg --gui
```
TraCI will connect directly to the real OSM corridor!
