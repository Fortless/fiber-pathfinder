import uvicorn
import requests
import networkx as nx
from scipy.spatial import KDTree
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import os
import math
import numpy as np

app = FastAPI()
os.makedirs("templates", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

ITU_WFS_URL = "https://bbmaps.itu.int/geoserver/ows"
SCM_CABLE_GEO = "https://www.submarinecablemap.com/api/v3/cable/cable-geo.json"
SCM_LANDING_GEO = "https://www.submarinecablemap.com/api/v3/landing-point/landing-point-geo.json"

def haversine(lon1, lat1, lon2, lat2):
    R = 6371
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

@app.get("/")
def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})



SCM_DATA = {
    "cables": requests.get("https://www.submarinecablemap.com/api/v3/cable/cable-geo.json").json(),
    "landings": requests.get("https://www.submarinecablemap.com/api/v3/landing-point/landing-point-geo.json").json()
}



@app.get("/calculate")
async def calculate_fiber(start_lat: float, start_lon: float, end_lat: float, end_lon: float):
    try:
        pad = 15.0
        bbox = f"{min(start_lon, end_lon)-pad},{min(start_lat, end_lat)-pad},{max(start_lon, end_lon)+pad},{max(start_lat, end_lat)+pad}"
        
        # fetch the data
        itu_params = {"service": "WFS", "version": "1.0.0", "request": "GetFeature",
                      "typeName": "ITU:trx_public_2", "outputFormat": "application/json", 
                      "maxFeatures": 40000, "bbox": bbox}
        itu_res = requests.get(ITU_WFS_URL, params=itu_params, timeout=40).json()
        scm_cables = requests.get(SCM_CABLE_GEO, timeout=40).json()
        scm_landings = requests.get(SCM_LANDING_GEO, timeout=40).json()

        G = nx.Graph()
        land_nodes = []
        sub_nodes = []

        # add land cables from bbmaps.itu.int
        for feat in itu_res.get('features', []):
            owner = feat['properties'].get('operator_l') or "Terrestrial Backbone"
            lines = [feat['geometry']['coordinates']] if feat['geometry']['type'] == 'LineString' else feat['geometry']['coordinates']
            for line in lines:
                for i in range(len(line)-1):
                    u, v = (round(line[i][0], 5), round(line[i][1], 5)), (round(line[i+1][0], 5), round(line[i+1][1], 5))
                    d = haversine(u[0], u[1], v[0], v[1])
                    G.add_edge(u, v, weight=d, actual_dist=d, owner=str(owner), type="land")
                    land_nodes.extend([u, v])

        # add submarine cables with 0.7 incentive
        for feat in scm_cables.get('features', []):
            owner = feat['properties'].get('name') or "Submarine Cable"
            lines = [feat['geometry']['coordinates']] if feat['geometry']['type'] == 'LineString' else feat['geometry']['coordinates']
            for line in lines:
                for i in range(len(line)-1):
                    u, v = (round(line[i][0], 5), round(line[i][1], 5)), (round(line[i+1][0], 5), round(line[i+1][1], 5))
                    # filter for bbox
                    if not (min(start_lon, end_lon)-25 < u[0] < max(start_lon, end_lon)+25): continue
                    d = haversine(u[0], u[1], v[0], v[1])
                    G.add_edge(u, v, weight=d * 0.7, actual_dist=d, owner=str(owner), type="submarine")
                    sub_nodes.extend([u, v])

        # bridge land points and submarine landing points
        if land_nodes and sub_nodes:
            land_pts = list(set(land_nodes))
            sub_pts = list(set(sub_nodes))
            land_tree = KDTree(land_pts)
            sub_tree = KDTree(sub_pts)

            # for every landing point in the global directory
            for feat in scm_landings.get('features', []):
                p = feat['geometry']['coordinates']
                p_coord = (round(p[0], 5), round(p[1], 5))

                # find nearest land fiber (within 50km)
                d_land, i_land = land_tree.query(p_coord, k=3, distance_upper_bound=0.5)
                # find nearest submarine coordinate (within 20km) - i.e. for mid-cable landings
                d_sub, i_sub = sub_tree.query(p_coord, k=1, distance_upper_bound=0.2)

                # connect them at the respective station
                if np.min(d_sub) != float('inf') and np.min(d_land) != float('inf'):
                    s_node = tuple(sub_tree.data[i_sub])
                    # Connect to up to 3 nearby land nodes to prevent bottlenecks
                    for d_l, idx_l in zip(np.atleast_1d(d_land), np.atleast_1d(i_land)):
                        if d_l != float('inf'):
                            l_node = tuple(land_tree.data[idx_l])
                            dist = haversine(s_node[0], s_node[1], l_node[0], l_node[1])
                            G.add_edge(s_node, l_node, weight=dist, actual_dist=dist, owner="Landing Station", type="bridge")

        # calc path
        all_nodes = list(G.nodes)
        full_tree = KDTree(all_nodes)
        start_node = all_nodes[full_tree.query((start_lon, start_lat))[1]]
        end_node = all_nodes[full_tree.query((end_lon, end_lat))[1]]

        path = nx.shortest_path(G, start_node, end_node, weight='weight')
        segments = []
        total_km = 0
        for i in range(len(path)-1):
            u, v = path[i], path[i+1]
            data = G.get_edge_data(u, v)
            total_km += data['actual_dist']
            segments.append({
                "coords": [[u[1], u[0]], [v[1], v[0]]],
                "owner": data['owner'], "type": data['type'], "dist": round(data['actual_dist'], 2)
            })

        return {
            "status": "success",
            "summary": {"total_km": round(total_km, 2), "rtt": round(total_km/100, 2)},
            "segments": segments,
            "partners": sorted(list(set([s['owner'] for s in segments])))
        }

    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
