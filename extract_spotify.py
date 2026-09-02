"""Run locally every 2-4 weeks. Dumps your Spotify taste to artists.json."""
import json, math, os, sys, time
import spotipy, yaml
from spotipy.oauth2 import SpotifyOAuth

CFG = yaml.safe_load(open("config.yml")) if os.path.exists("config.yml") else {}
PL_WEIGHTS = {k.lower(): v for k, v in (CFG.get("playlists") or {}).items()}
PL_DEFAULT = CFG.get("playlist_default_weight", 0)
NON_ELEC   = CFG.get("non_electronic_mult", 0.35)
TOPN       = int(CFG.get("metadata_top", 1000))
OVR = ({str(k).lower(): float(v) for k, v in (yaml.safe_load(open("taste_overrides.yml")) or {}).items()}
       if os.path.exists("taste_overrides.yml") else {})

ELEC_KEYWORDS = ("house techno edm electro dance rave club bass dubstep dnb "
    "drum and bass jungle garage grime trance hardstyle hardcore gabber breakbeat "
    "breakcore footwork juke jersey bounce electronic electronica idm ambient "
    "downtempo hyperpop glitch synth big room future disco italo acid amapiano "
    "baile funk").split()

class Quota(Exception): pass

sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
        scope=("user-library-read user-top-read user-read-recently-played "
               "playlist-read-private"),
        redirect_uri="http://127.0.0.1:8888/callback"),
    retries=0)

def call(fn, *a, **kw):
    """Retry transient errors, wait out short rate limits, stop clean on quota."""
    for attempt in range(4):
        try:
            return fn(*a, **kw)
        except spotipy.SpotifyException as e:
            if e.http_status == 429:
                wait = int(float((e.headers or {}).get("Retry-After", 2)))
                if wait > 120:
                    raise Quota(wait)
                time.sleep(wait + 1)
            elif e.http_status and e.http_status >= 500 and attempt < 3:
                time.sleep(3 * (attempt + 1))
            else:
                raise
    raise RuntimeError("request kept failing after retries")

def run():
    artists = {}
    def touch(a):
        return artists.setdefault(a["id"], {"name": a["name"], "liked": 0, "top": {},
                                            "toptrack": {}, "recent": 0, "pl": {}})

    # 1) liked songs
    offset = 0
    while True:
        page = call(sp.current_user_saved_tracks, limit=50, offset=offset)
        for item in page["items"]:
            if item.get("track"):
                for a in item["track"]["artists"]:
                    if a.get("id"): touch(a)["liked"] += 1
        offset += 50
        print(f"\r  liked tracks {min(offset, page['total'])}/{page['total']}", end="")
        if page["next"] is None: break

    # 2) top artists + top tracks, recency-forward
    TERMS = {"long_term": 1.0, "medium_term": 1.25, "short_term": 1.5}
    for term in TERMS:
        for rank, a in enumerate(call(sp.current_user_top_artists, limit=50, time_range=term)["items"]):
            touch(a)["top"][term] = rank
        for rank, t in enumerate(call(sp.current_user_top_tracks, limit=50, time_range=term)["items"]):
            for a in t["artists"]:
                if a.get("id"):
                    d = touch(a)
                    d["toptrack"][term] = min(d["toptrack"].get(term, rank), rank)

    # 3) last 50 plays
    for item in call(sp.current_user_recently_played, limit=50)["items"]:
        for a in item["track"]["artists"]:
            if a.get("id"): touch(a)["recent"] += 1

    # 4) playlists: only solo-owned ones count toward taste
    me = call(sp.me)["id"]
    print("\n  playlists (solo-owned get scanned, shared ones are skipped):")
    seen_pl = set()
    offset = 0
    while True:
        page = call(sp.current_user_playlists, limit=50, offset=offset)
        for pl in page["items"]:
            if not pl or not pl.get("id") or not pl.get("name"): continue
            if pl["id"] in seen_pl: continue
            seen_pl.add(pl["id"])
            mine = (pl.get("owner") or {}).get("id") == me and not pl.get("collaborative")
            w = PL_WEIGHTS.get(pl["name"].lower(), PL_DEFAULT)
            total = ((pl.get("tracks") or pl.get("items") or {}).get("total", "?"))
            note = "" if mine else "   [shared: skipped]"
            print(f'    [{w:>4}] {pl["name"]} ({total} tracks){note}')
            if not mine or w <= 0: continue
            po = 0
            try:
                while True:
                    items = call(sp.playlist_items, pl["id"], limit=100, offset=po,
                                 additional_types=("track",))
                    for it in items["items"]:
                        tr = it.get("track") or it.get("item")
                        if tr and tr.get("artists"):
                            for a in tr["artists"]:
                                if a.get("id"):
                                    d = touch(a)
                                    d["pl"][pl["name"]] = d["pl"].get(pl["name"], 0) + 1
                    po += 100
                    if items["next"] is None: break
            except Quota:
                raise
            except Exception:
                print("      ^ skipped: Spotify refused this one")
        offset += 50
        if page["next"] is None: break

    def base(d):
        s = math.log1p(d["liked"])
        for term, w in TERMS.items():
            if term in d["top"]:
                s += w * 3.0 * (50 - d["top"][term]) / 50
            if term in d["toptrack"]:
                s += w * 1.5 * (50 - d["toptrack"][term]) / 50
        s += min(1.5, 0.3 * d["recent"])
        for name, count in d["pl"].items():
            s += PL_WEIGHTS.get(name.lower(), PL_DEFAULT) * math.log1p(count)
        return s

    # 5) genre metadata: singles, cached forever, top slice only, quota-aware
    CACHE = "artist_meta.json"
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    ranked = sorted(artists, key=lambda i: -base(artists[i]))
    targets = [i for i in ranked[:TOPN] if i not in cache]
    fails, quota_wait = 0, None
    print(f"\n  metadata: {len(targets)} artists to fetch, {len(cache)} already cached")
    try:
        for n, aid in enumerate(targets, 1):
            try:
                cache[aid] = {"genres": call(sp.artist, aid).get("genres", [])}
            except Quota as q:
                quota_wait = q.args[0]
                break
            except Exception:
                fails += 1
            time.sleep(0.3)
            if n % 25 == 0:
                json.dump(cache, open(CACHE, "w"))
                print(f"\r  metadata {n}/{len(targets)}", end="")
    finally:
        json.dump(cache, open(CACHE, "w"))
    for aid, meta in cache.items():
        if aid in artists:
            artists[aid]["genres"] = meta.get("genres", [])
    covered = sum(1 for i in ranked[:TOPN] if i in cache)
    if quota_wait:
        print(f"\n  daily quota hit; progress saved ({covered}/{TOPN} covered).")
        print(f"  resumes automatically next run, quota resets in ~{quota_wait/3600:.0f}h")

    # 6) score
    def context_mult(d):
        if d["name"].lower() in OVR:
            return OVR[d["name"].lower()]
        genres = d.get("genres", [])
        if not genres:
            return 1.0
        return 1.0 if any(k in g for g in genres for k in ELEC_KEYWORDS) else NON_ELEC

    out = []
    for i, d in artists.items():
        m = context_mult(d)
        out.append({"id": i, "name": d["name"], "liked": d["liked"],
                    "aff": round(base(d) * m, 2), "mult": m,
                    "genres": d.get("genres", [])[:3]})
    out.sort(key=lambda a: -a["aff"])
    json.dump(out, open("artists.json", "w"), indent=1)

    label = "provisional" if (quota_wait or fails > 25) else "final"
    print(f"\n\n{len(out)} artists written ({covered}/{TOPN} genre coverage, {label}). Top 25:\n")
    for a in out[:25]:
        g = a["genres"][0] if a["genres"] else "no genre data"
        print(f'  {a["aff"]:6.2f}  {a["name"]}  (x{a["mult"]}, {g})')

if __name__ == "__main__":
    try:
        run()
    except Quota as q:
        h = (q.args[0] / 3600) if q.args else 24
        print(f"\n\n  Spotify's daily request quota is already spent; it resets in ~{h:.0f}h.")
        print("  Nothing was corrupted. Run again after the reset.")
