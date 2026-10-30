"""Read-only D1 reachability probe: baseline vs. shipped surface.

Run with: python scripts/d1_reachability_probe.py
Prints, for every curated claim, the engine outcome and the HTTP response code
obtained through the real ASGI app (no network, no provider calls).
"""
from __future__ import annotations

import asyncio
import json

import httpx

from civictrace.app import app, court


async def main() -> None:
    print(f'claims_in_book={len(court.claims)}')
    print(f'{"claim_id":24} {"http":>5}  {"engine_status":36} reason_codes')
    statuses_http = set()
    reachable = 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        for claim_id in court.claims:
            engine = court.evaluate(claim_id)
            response = await client.post('/api/challenge', json={'claim_id': claim_id})
            if response.status_code == 200:
                reachable += 1
                statuses_http.add(response.json()['status'])
            reasons = ','.join(engine.reason_codes)
            print(f'{claim_id:24} {response.status_code:>5}  {engine.status:36} {reasons}')
        case = await client.get('/api/case')
        advertised = [c['id'] for c in case.json()['claims']]
        index = await client.get('/')
    print(f'http_reachable_claim_ids={reachable}/{len(court.claims)}')
    print(f'engine_statuses_all={sorted({court.evaluate(c).status for c in court.claims})}')
    print(f'http_statuses_reachable={sorted(statuses_http)}')
    print(f'advertised_claim_ids={len(advertised)} vote_5_0_advertised={"vote_5_0" in advertised}')
    print(f'ui_offers_vote_5_0={"data-claim=" + chr(34) + "vote_5_0" + chr(34) in index.text}')
    print(f'ui_offers_vote_4_0={"data-claim=" + chr(34) + "vote" + chr(34) in index.text}')


if __name__ == '__main__':
    asyncio.run(main())
