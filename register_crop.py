"""Register a locally downloaded NGII raster and crop one verified WGS84 administrative polygon."""
import argparse,hashlib,json,shutil
from pathlib import Path
from datetime import datetime,timezone
import numpy as np
import rasterio
from rasterio.mask import mask
from rasterio.warp import transform_geom

def sha(path):
    h=hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):h.update(chunk)
    return h.hexdigest()

def run(image,boundary,metadata,out):
    meta=json.loads(Path(metadata).read_text(encoding="utf-8"))
    required=["product_id","observation_time","bands","source_url","admin_dong_code","admin_boundary_version"]
    if any(not meta.get(k) for k in required):raise ValueError("Complete metadata: "+", ".join(required))
    if datetime.fromisoformat(meta["observation_time"].replace("Z","+00:00")).tzinfo is None:
        raise ValueError("observation_time requires timezone")
    geo=json.loads(Path(boundary).read_text(encoding="utf-8"))
    features=geo.get("features",[])
    if len(features)!=1 or features[0]["geometry"]["type"] not in ("Polygon","MultiPolygon"):
        raise ValueError("Boundary must be a WGS84 FeatureCollection with one verified administrative polygon")
    declared=geo.get("crs",{}).get("properties",{}).get("name","EPSG:4326")
    if not ("4326" in declared or "CRS84" in declared):raise ValueError("Boundary must be WGS84")
    # The caller must verify that this polygon is the named administrative dong.
    out=Path(out);bronze=out/"bronze";originals=bronze/"originals";silver=out/"silver"
    for folder in (originals,bronze,silver):folder.mkdir(parents=True,exist_ok=True)
    target=originals/Path(image).name
    if target.exists() and sha(target)!=sha(image):raise ValueError("Existing Bronze original differs; use separate output folder")
    if not target.exists():shutil.copy2(image,target)
    with rasterio.open(target) as src:
        if src.crs is None:raise ValueError("A georeferenced raster is required")
        if len(meta["bands"])!=src.count:raise ValueError("Verify actual band count/order in product metadata")
        shape=transform_geom("EPSG:4326",src.crs,features[0]["geometry"])
        data,transform=mask(src,[shape],crop=True,filled=False)
        valid=~np.any(np.ma.getmaskarray(data),axis=0)
        if not valid.any():raise ValueError("No valid pixel inside boundary")
        profile=src.profile.copy()
        profile.update(driver="GTiff",height=data.shape[1],width=data.shape[2],transform=transform)
        path=silver/"admin_crop.tif"
        if path.exists():raise ValueError("Crop exists; use another output folder for another processing run")
        with rasterio.open(path,"w",**profile) as dst:
            dst.write(data.filled(src.nodata if src.nodata is not None else 0))
            dst.write_mask(valid.astype("uint8")*255)
        record={**meta,"native_crs":str(src.crs),"pixel_size_crs_units":list(src.res),
                "collected_at":datetime.now(timezone.utc).isoformat(),"collection_time_basis":"local registration, not original download",
                "bronze_file":str(target),"sha256":sha(target),"boundary_path":str(boundary),
                "processed_path":str(path),"processed_sha256":sha(path),"transform":list(transform)[:6],
                "shape":list(data.shape),"valid_pixel_count":int(valid.sum()),
                "cloud_qc_status":"unverified","processing":"geometry+source nodata mask only; native sampling preserved"}
    (bronze/"asset_manifest.json").write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")
    (silver/"crop_index.json").write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding="utf-8")
    print("Registered and cropped:",path)

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    for arg in ["image","boundary","metadata","out"]:p.add_argument("--"+arg,required=True)
    a=p.parse_args();run(a.image,a.boundary,a.metadata,a.out)
