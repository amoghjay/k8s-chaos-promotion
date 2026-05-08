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
const SIGNER_URL        = __ENV.SIGNER_URL  || 'http://radius-signer.url-shortener-staging.svc.cluster.local:8080';
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

const thresholds = {
  // Staging is now functionally healthy, but payment verification still adds
  // real chain latency. Keep the gate meaningful without failing good runs.
  http_req_duration: ['p(95)<1200'],
  http_req_failed:   ['rate<0.05'],
  shorten_201_rate:  ['rate>0.90'],
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

  const signerHealth = http.get(`${SIGNER_URL}/health`, { timeout: PRECHECK_TIMEOUT });
  validateHttpResponse(signerHealth, `SIGNER_URL health check failed for ${SIGNER_URL}/health`);

  let signerPayload;
  try {
    signerPayload = JSON.parse(signerHealth.body);
  } catch (_) {
    fail(`Signer health check returned non-JSON response from ${SIGNER_URL}`);
  }

  if ((signerPayload.wallet_count || 0) < VUS) {
    fail(`Signer has ${signerPayload.wallet_count || 0} wallets configured but VUS=${VUS}`);
  }
}

// ── Main loop ─────────────────────────────────────────────────────────────────
export default function () {
  // Step 1: Request a real SBC transfer from the signer service
  let txHash = null;
  if (PAYMENT_ENABLED) {
    const txStartedAt = Date.now();
    const payResponse = http.post(
      `${SIGNER_URL}/pay`,
      JSON.stringify({
        wallet_index: __VU - 1,
        amount: SBC_AMOUNT,
        gas_limit: TX_GAS_LIMIT,
      }),
      {
        headers: { 'Content-Type': 'application/json' },
        timeout: `${RECEIPT_TIMEOUT_S + 5}s`,
      },
    );

    if (payResponse.status !== 200) {
      console.error(`VU${__VU} signer payment failed: status=${payResponse.status} body=${payResponse.body}`);
      txSubmitSuccessRate.add(false);
      txReceiptSuccessRate.add(false);
      return;
    }

    let payResult;
    try {
      payResult = JSON.parse(payResponse.body);
      txSubmitSuccessRate.add(true);
    } catch (e) {
      console.error(`VU${__VU} signer returned invalid JSON: ${e}`);
      txSubmitSuccessRate.add(false);
      txReceiptSuccessRate.add(false);
      return;
    }

    txHash = payResult.tx_hash;
    if (!txHash) {
      console.error(`VU${__VU} signer response did not include tx_hash`);
      txReceiptSuccessRate.add(false);
      return;
    }

    txReceiptSuccessRate.add(true);
    txConfirmationMs.add(payResult.confirmation_ms || (Date.now() - txStartedAt));
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

  if (shorten.status !== 201) {
    console.error(
      `VU${__VU} shorten failed: status=${shorten.status} tx_hash=${txHash || 'none'} body=${shorten.body}`
    );
  }

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
