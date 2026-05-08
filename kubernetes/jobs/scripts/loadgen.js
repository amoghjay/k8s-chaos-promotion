import { Client } from 'k6/x/ethereum';
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend } from 'k6/metrics';

// ── Custom metrics ────────────────────────────────────────────────────────────
const txSubmitSuccessRate  = new Rate('tx_submit_success_rate');
const txReceiptSuccessRate = new Rate('tx_receipt_success_rate');
const txConfirmationMs     = new Trend('tx_confirmation_ms', true);
const shorten201Rate       = new Rate('shorten_201_rate');
const shorten402Rate       = new Rate('shorten_402_rate');
const shorten409Rate       = new Rate('shorten_409_rate');
const shorten5xxRate       = new Rate('shorten_5xx_rate');
const redirectOkRate       = new Rate('redirect_ok_rate');

// ── ENV vars ──────────────────────────────────────────────────────────────────
const BASE_URL          = __ENV.BASE_URL    || 'http://url-shortener-staging.url-shortener-staging.svc.cluster.local:80';
const RPC_URL           = __ENV.RPC_URL     || 'https://rpc.testnet.radiustech.xyz';
const CHAIN_ID          = parseInt(__ENV.CHAIN_ID || '72344');
const SERVICE_WALLET    = __ENV.SERVICE_WALLET_ADDRESS;
const SBC_CONTRACT      = __ENV.SBC_CONTRACT_ADDRESS || '0x33ad9e4bd16b69b5bfded37d8b5d9ff9aba014fb';
const SBC_AMOUNT        = parseInt(__ENV.SBC_AMOUNT || '1000'); // 0.001 SBC (6 decimals)
const PAYMENT_ENABLED   = (__ENV.PAYMENT_ENABLED || 'true') === 'true';
const VUS               = parseInt(__ENV.VUS || '3');
const LOAD_PROFILE      = __ENV.LOAD_PROFILE || 'arrival-rate';
const ARRIVAL_RATE      = parseInt(__ENV.ARRIVAL_RATE || '60');
const ARRIVAL_TIME_UNIT = __ENV.ARRIVAL_TIME_UNIT || '1m';
const SLEEP_SECONDS     = parseFloat(__ENV.SLEEP_SECONDS || '2');
const RECEIPT_TIMEOUT_S = parseInt(__ENV.RECEIPT_TIMEOUT_S || '30');
const PRECHECK_TIMEOUT  = __ENV.PRECHECK_TIMEOUT || '5s';
const DURATION          = __ENV.DURATION || '5m';
const TX_GAS_LIMIT      = parseInt(__ENV.TX_GAS_LIMIT || '100000');

// One key per VU — avoids nonce conflicts. VU1→KEY_1, VU2→KEY_2, VU3→KEY_3.
const WALLET_KEYS = [
  __ENV.WALLET_KEY_1,
  __ENV.WALLET_KEY_2,
  __ENV.WALLET_KEY_3,
];

const thresholds = {
  http_req_duration: ['p(95)<500'],
  http_req_failed:   ['rate<0.05'],
  shorten_201_rate:  ['rate>0.95'],
  redirect_ok_rate:  ['rate>0.95'],
};

if (PAYMENT_ENABLED) {
  thresholds.tx_submit_success_rate = ['rate>0.95'];
  thresholds.tx_receipt_success_rate = ['rate>0.95'];
}

// ── Options ───────────────────────────────────────────────────────────────────
export const options = LOAD_PROFILE === 'arrival-rate'
  ? {
      scenarios: {
        steady_payments: {
          executor: 'constant-arrival-rate',
          rate: ARRIVAL_RATE,
          timeUnit: ARRIVAL_TIME_UNIT,
          duration: DURATION,
          preAllocatedVUs: VUS,
          maxVUs: VUS,
        },
      },
      thresholds,
    }
  : {
      vus: VUS,
      duration: DURATION,
      thresholds,
    };

function fail(message) {
  throw new Error(message);
}

function validateHttpResponse(response, message) {
  if (response.status !== 200) {
    fail(`${message} (status=${response.status})`);
  }
}

export function setup() {
  const health = http.get(`${BASE_URL}/health`, { timeout: PRECHECK_TIMEOUT });
  validateHttpResponse(health, `BASE_URL health check failed for ${BASE_URL}/health`);

  if (!PAYMENT_ENABLED) {
    return;
  }

  if (!SERVICE_WALLET) {
    fail('SERVICE_WALLET_ADDRESS is required when PAYMENT_ENABLED=true');
  }

  const configuredKeys = WALLET_KEYS.filter(Boolean);
  if (configuredKeys.length < VUS) {
    fail(`Configured ${configuredKeys.length} wallet keys but VUS=${VUS}; provide one wallet per VU`);
  }

  const rpcCheck = http.post(
    RPC_URL,
    JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'eth_chainId',
      params: [],
    }),
    {
      headers: { 'Content-Type': 'application/json' },
      timeout: PRECHECK_TIMEOUT,
    },
  );
  validateHttpResponse(rpcCheck, `RPC precheck failed for ${RPC_URL}`);

  let payload;
  try {
    payload = JSON.parse(rpcCheck.body);
  } catch (_) {
    fail(`RPC precheck returned non-JSON response from ${RPC_URL}`);
  }

  if (payload.error) {
    fail(`RPC precheck returned error: ${JSON.stringify(payload.error)}`);
  }

  const actualChainId = parseInt(payload.result, 16);
  if (actualChainId !== CHAIN_ID) {
    fail(`RPC chain ID mismatch: expected ${CHAIN_ID}, got ${actualChainId}`);
  }
}

// ── Per-VU lazy init — created on first iteration per VU ──────────────────────
let client = null;
function getClient() {
  if (!PAYMENT_ENABLED) {
    return null;
  }

  if (client) {
    return client;
  }

  const myKey = WALLET_KEYS[__VU - 1];
  if (!myKey) {
    fail(`WALLET_KEY_${__VU} is not set — fund one wallet per VU`);
  }

  client = new Client({
    url:        RPC_URL,
    privateKey: myKey, // no 0x prefix
  });

  return client;
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
  // Step 1: Sign and broadcast SBC transfer on Radius testnet
  let txHash = null;
  if (PAYMENT_ENABLED) {
    const vuClient = getClient();
    const txStartedAt = Date.now();

    try {
      txHash = vuClient.sendRawTransaction({
        to:   SBC_CONTRACT,
        gas:  TX_GAS_LIMIT,
        chain_id: CHAIN_ID,
        input: encodeERC20Transfer(SERVICE_WALLET, SBC_AMOUNT),
      });
      txSubmitSuccessRate.add(true);
    } catch (e) {
      console.error(`VU${__VU} tx submit failed: ${e}`);
      txSubmitSuccessRate.add(false);
      txReceiptSuccessRate.add(false);
      return;
    }

    try {
      // Radius has sub-second finality but eth_getTransactionReceipt returns null
      // until confirmed — wait before submitting to avoid spurious 402s from the app.
      vuClient.waitForTransactionReceipt(txHash, RECEIPT_TIMEOUT_S);
      txReceiptSuccessRate.add(true);
      txConfirmationMs.add(Date.now() - txStartedAt);
    } catch (e) {
      console.error(`VU${__VU} tx receipt wait failed: ${e}`);
      txReceiptSuccessRate.add(false);
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
  shorten201Rate.add(shorten.status === 201);
  shorten402Rate.add(shorten.status === 402);
  shorten409Rate.add(shorten.status === 409);
  shorten5xxRate.add(shorten.status >= 500 && shorten.status < 600);

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

  if (LOAD_PROFILE === 'closed-loop') {
    sleep(SLEEP_SECONDS);
  }
}
