"""First authenticated request; preserves original bytes in Bronze, NOT a complete collection pipeline."""
import argparse, hashlib, json, os, time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
import requests
from dotenv import load_dotenv

def read_config(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def run(config, out, start=None, end=None):
    load_dotenv(".env")
    endpoint=config["endpoint"]
    if urlparse(endpoint).scheme!="https" or urlparse(endpoint).query:
        raise ValueError("Use an HTTPS endpoint without query string or credentials")
    params=dict(config.get("params", {}))
    env_name=config.get("key_env")
    if env_name:
        key=os.environ.get(env_name)
        if not key: raise ValueError("Missing environment variable: "+env_name)
        if config["key_param"] in params: raise ValueError("Do not put keys in config")
    else: key=None
    slots=[None]
    if start or end:
        if not start or not end: raise ValueError("Both start and end required")
        a=datetime.fromisoformat(start); b=datetime.fromisoformat(end)
        if a.tzinfo is None or b.tzinfo is None: raise ValueError("Include timezone offset")
        if a>b or a.minute or b.minute or a.second or b.second: raise ValueError("Use aligned hourly times")
        count=int((b-a).total_seconds()/3600)+1
        if count>744: raise ValueError("Maximum 744 requests; split longer ranges")
        slots=[a+timedelta(hours=i) for i in range(count)]
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    with requests.Session() as session:
        for index,slot in enumerate(slots):
            safe=dict(params)
            if slot:
                safe[config.get("time_param","tm")]=slot.strftime(config.get("time_format","%Y%m%d%H%M"))
            secret=dict(safe)
            if key: secret[config["key_param"]]=key
            record={"endpoint":endpoint,"request_parameters":safe,"requested_time":slot.isoformat() if slot else None,
                    "collected_at":datetime.now(timezone.utc).isoformat(),"semantic_qc":"unverified"}
            response=None
            for attempt in range(3):
                try:
                    response=session.get(endpoint,params=secret,timeout=(10,60))
                    if response.status_code not in (429,500,502,503,504): break
                except requests.RequestException:
                    response=None
                time.sleep(2**attempt)
            if response is None: record["status"]="network_failure"
            else:
                record.update(http_status=response.status_code,size_bytes=len(response.content))
                ext=config.get("extension","txt").lstrip(".")
                stem=slot.strftime("%Y%m%d%H%M%z") if slot else str(index+1).zfill(4)
                payload=out/(stem+"."+ext)
                if payload.exists() and payload.read_bytes()!=response.content:
                    payload=out/(stem+"_"+datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")+"."+ext)
                if not payload.exists(): payload.write_bytes(response.content)
                record.update(bronze_file=str(payload),sha256=hashlib.sha256(response.content).hexdigest(),
                              status="downloaded" if response.status_code==200 and response.content else "http_or_empty_failure")
                # A returned key must not enter metadata or console.
                if key: record=json.loads(json.dumps(record).replace(key,"[REDACTED]"))
            with (out/"manifest.jsonl").open("a",encoding="utf-8") as stream:
                stream.write(json.dumps(record,ensure_ascii=False)+"\n")
            print(index+1,record["status"])
            if record["status"]!="downloaded": raise RuntimeError("Request failed; inspect saved status, not secret URL")
    return len(slots)

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config",required=True);p.add_argument("--out",required=True)
    p.add_argument("--start");p.add_argument("--end")
    args=p.parse_args()
    try:run(read_config(args.config),args.out,args.start,args.end)
    except (ValueError,RuntimeError) as exc:p.exit(1,str(exc)+"\n")
