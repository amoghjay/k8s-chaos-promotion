import { Client } from 'k6/x/ethereum';
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate } from 'k6/metrics';

// ── Custom metrics ────────────────────────────────────────────────────────────
const paymentSuccessRate = new Rate('payment_success_rate');
const payment402Rate     = new Rate('payment_402_rate');
const payment409Rate     = new Rate('payment_409_rate');
const redirectOkRate     = new Rate('redirect_ok_rate');

// ── ENV vars ──────────────────────────────────────────────────────────────────
const BASE_URL          = __ENV.BASE_URL    || 'http://url-shortener-staging.url-shortener-staging.svc.cluster.local:80';
const RPC_URL           = __ENV.RPC_URL     || 'https://rpc.testnet.radiustech.xyz';
const CHAIN_ID          = parseInt(__ENV.CHAIN_ID || '72344');
const SERVICE_WALLET    = __ENV.SERVICE_WALLET_ADDRESS;
const SBC_CONTRACT      = '0x33ad9e4bd16b69b5bfded37d8b5d9ff9aba014fb';
const SBC_AMOUNT        = 1000; // 0.001 SBC (6 decimals)
const PAYMENT_ENABLED   = (__ENV.PAYMENT_ENABLED || 'true') === 'true';

// One key per VU — avoids nonce conflicts. VU1→KEY_1, VU2→KEY_2, VU3→KEY_3.
const WALLET_KEYS = [
  __ENV.WALLET_KEY_1,
  __ENV.WALLET_KEY_2,
  __ENV.WALLET_KEY_3,
];

// ── Options ───────────────────────────────────────────────────────────────────
export const options = {
  vus:      parseInt(__ENV.VUS || '3'),
  duration: __ENV.DURATION     || '5m',
  thresholds: {
    http_req_duration:    ['p(95)<500'],
    http_req_failed:      ['rate<0.05'],
    payment_success_rate: ['rate>0.95'],
  },
};

// ── Per-VU init (module-level — runs once per VU before the test loop) ────────
// When PAYMENT_ENABLED=false the client is skipped entirely; wallets not needed.
let client = null;
if (PAYMENT_ENABLED) {
  const myKey = WALLET_KEYS[__VU - 1];
  if (!myKey) {
    throw new Error(`WALLET_KEY_${__VU} is not set — fund one wallet per VU`);
  }
  client = new Client({
    url:        RPC_URL,
    privateKey: myKey, // no 0x prefix
    chainID:    CHAIN_ID,
  });
}

// ── ABI encode ERC-20 transfer(address,uint256) ───────────────────────────────
function encodeERC20Transfer(to, amount) {
  const selector  = 'a9059cbb';
  const paddedTo  = to.replace('0x', '').toLowerCase().padStart(64, '0');
  const paddedAmt = parseInt(amount).toString(16).padStart(64, '0');
  return '0x' + selector + paddedTo + paddedAmt;
}

// ── Main loop ─────────────────────────────────────────────────────────────────
export default function () {
  if (!SERVICE_WALLET) {
    console.error('SERVICE_WALLET_ADDRESS is required');
    return;
  }

  // Step 1: Sign and broadcast SBC transfer on Radius testnet
  let txHash = null;
  if (PAYMENT_ENABLED) {
    try {
      txHash = client.sendRawTransaction({
        to:   SBC_CONTRACT,
        gas:  100000,
        data: encodeERC20Transfer(SERVICE_WALLET, SBC_AMOUNT),
      });
      // Radius has sub-second finality but eth_getTransactionReceipt returns null
      // until confirmed — wait before submitting to avoid spurious 402s from the app.
      client.waitForTransactionReceipt(txHash, 30);
    } catch (e) {
      console.error(`VU${__VU} tx failed: ${e}`);
      paymentSuccessRate.add(false);
      return;
    }
  }

  // Step 2: POST /shorten (with tx_hash if payment enabled)
  const payload = { url: `https://example.com/load-test-${__VU}-${Date.now()}` };
  if (txHash) payload.tx_hash = txHash;

  const shorten = http.post(
    `${BASE_URL}/shorten`,
    JSON.stringify(payload),
    { headers: { 'Content-Type': 'application/json' } },
  );

  check(shorten, { 'shorten 201': (r) => r.status === 201 });
  paymentSuccessRate.add(shorten.status === 201);
  payment402Rate.add(shorten.status === 402);
  payment409Rate.add(shorten.status === 409);

  if (shorten.status === 409) {
    console.warn(`VU${__VU} 409 replay — unexpected tx_hash reuse: ${txHash}`);
  }

  // Step 3: GET /{code} — verify redirect (no follow)
  if (shorten.status === 201) {
    let code = '';
    try { code = JSON.parse(shorten.body).code || JSON.parse(shorten.body).short_code || ''; } catch (_) {}

    if (code) {
      const redirect = http.get(`${BASE_URL}/${code}`, { redirects: 0 });
      check(redirect, { 'redirect 302': (r) => r.status === 302 });
      redirectOkRate.add(redirect.status === 302);
    }
  }

  sleep(2);
}
