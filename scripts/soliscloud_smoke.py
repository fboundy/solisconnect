"""Live smoke test for the SolisCloud API client. NOT collected by pytest/CI.

Reads credentials from a Home Assistant secrets.yaml (keys prefixed `soliscloud_`)
and exercises the read path against the real API:
  login -> inverterList -> inverterDetail -> atRead(636) -> atReadBatch(636, 157)

Usage:
    uv run python scripts/soliscloud_smoke.py --secrets /config/secrets.yaml
    (on the dev machine the HA config share is mounted as i:\\, so --secrets i:\\secrets.yaml)
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiohttp
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.solisconnect.cloud.api_client import SolisCloudApiClient  # noqa: E402


def load_credentials(secrets_path: str) -> dict:
    with open(secrets_path, encoding="utf-8") as f:
        secrets = yaml.safe_load(f)
    keys = ["soliscloud_username", "soliscloud_password", "soliscloud_key_id", "soliscloud_key_secret", "soliscloud_plant_id"]
    missing = [k for k in keys if k not in secrets]
    if missing:
        raise SystemExit(f"Missing keys in {secrets_path}: {missing}")
    return {k.removeprefix("soliscloud_"): str(secrets[k]) for k in keys}


async def main(secrets_path: str) -> int:
    creds = load_credentials(secrets_path)

    # Thread-based resolver: aiodns (the default) is unreliable on Windows event loops
    connector = aiohttp.TCPConnector(resolver=aiohttp.resolver.ThreadedResolver())
    async with aiohttp.ClientSession(connector=connector) as session:
        client = SolisCloudApiClient(
            session=session,
            key_id=creds["key_id"],
            key_secret=creds["key_secret"],
            username=creds["username"],
            password=creds["password"],
        )

        print("== login ==")
        token = await client.async_login()
        print(f"csrfToken: {token[:8]}... (cached)")

        print("\n== inverterList ==")
        records = await client.async_inverter_list(creds["plant_id"])
        if not records:
            print("No inverters found for this plant id!")
            return 1
        for rec in records:
            print(f"id={rec.get('id')} sn={rec.get('sn')} model={rec.get('inverterType', rec.get('model', '?'))} state={rec.get('state')}")
        inverter_sn = str(records[0]["sn"])
        inverter_id = str(records[0]["id"])

        print(f"\n== inverterDetail ({inverter_sn}) ==")
        detail = await client.async_inverter_detail(inverter_sn, inverter_id)
        interesting = [
            "pac",
            "pacStr",
            "eToday",
            "eTotal",
            "batteryCapacitySoc",
            "batteryPower",
            "batteryPowerStr",
            "inverterTemperature",
            "fac",
            "currentState",
            "psum",
            "psumStr",
            "familyLoadPower",
            "uPv1",
            "iPv1",
        ]
        for key in interesting:
            if key in detail:
                print(f"  {key}: {detail[key]}")
        print(f"  ({len(detail)} fields total)")

        print("\n== atRead(cid=636, storage mode) ==")
        mode = await client.async_at_read(inverter_sn, 636)
        print(f"  cid 636 = {mode}")

        print("\n== atReadBatch(636, 157) ==")
        batch = await client.async_at_read_batch(inverter_sn, [636, 157])
        print(f"  {batch}")

        dump_path = Path(__file__).parent / "soliscloud_detail_dump.json"
        dump_path.write_text(json.dumps(detail, indent=2), encoding="utf-8")
        print(f"\nFull inverterDetail dumped to {dump_path} (for mapping work; gitignored)")

    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        # aiodns (aiohttp's default resolver) requires a selector event loop on Windows
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secrets", required=True, help="Path to Home Assistant secrets.yaml")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.secrets)))
