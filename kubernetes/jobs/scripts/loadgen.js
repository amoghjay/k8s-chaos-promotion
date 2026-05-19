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
const TURNSTILE_MIN_SBC_UNITS = parseInt(__ENV.TURNSTILE_MIN_SBC_UNITS || '100000');
const MIN_WALLET_START_BALANCE = parseInt(__ENV.MIN_WALLET_START_BALANCE || '0');
const PRECHECK_BALANCE_BUFFER_PAYMENTS = parseInt(__ENV.PRECHECK_BALANCE_BUFFER_PAYMENTS || '5');
const SHORTEN_RETRY_ATTEMPTS = Math.max(1, parseInt(__ENV.SHORTEN_RETRY_ATTEMPTS || '3'));
const SHORTEN_RETRY_DELAY_MS = Math.max(0, parseInt(__ENV.SHORTEN_RETRY_DELAY_MS || '300'));

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

function parseDurationMs(value) {
  const match = /^(\d+(?:\.\d+)?)(ms|s|m|h)$/.exec(String(value).trim());
  if (!match) {
    fail(`Unsupported duration format: ${value}`);
  }

  const amount = parseFloat(match[1]);
  const unit = match[2];
  const multipliers = { ms: 1, s: 1000, m: 60000, h: 3600000 };
  return amount * multipliers[unit];
}

function estimateRequiredWalletBalance() {
  if (MIN_WALLET_START_BALANCE > 0) {
    return MIN_WALLET_START_BALANCE;
  }

  const durationMs = parseDurationMs(DURATION);
  let expectedPaymentsPerWallet = 1;

  if (LOAD_PROFILE === 'arrival-rate') {
    const timeUnitMs = parseDurationMs(ARRIVAL_TIME_UNIT);
    const totalPayments = Math.ceil((durationMs / timeUnitMs) * ARRIVAL_RATE);
    expectedPaymentsPerWallet = Math.max(1, Math.ceil(totalPayments / VUS));
  } else {
    const cycleSeconds = Math.max(SLEEP_SECONDS, 0.1);
    expectedPaymentsPerWallet = Math.max(1, Math.ceil(durationMs / (cycleSeconds * 1000)));
  }

  return (expectedPaymentsPerWallet + PRECHECK_BALANCE_BUFFER_PAYMENTS) * SBC_AMOUNT;
}

function parseJsonBody(body, fallbackMessage) {
  try {
    return JSON.parse(body);
  } catch (_) {
    return fallbackMessage ? { detail: fallbackMessage } : null;
  }
}

function shouldRetryShorten(response) {
  if (response.status !== 503 && response.status !== 400) {
    return false;
  }

  const payload = parseJsonBody(response.body) || {};
  const detail = String(payload.detail || payload.message || '').toLowerCase();
  return (
    detail.includes('transaction receipt not yet visible') ||
    detail.includes('transaction not found') ||
    detail.includes('not yet confirmed') ||
    detail.includes('retry shortly')
  );
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

  const walletsResponse = http.get(`${SIGNER_URL}/wallets`, { timeout: PRECHECK_TIMEOUT });
  validateHttpResponse(walletsResponse, `SIGNER_URL wallet precheck failed for ${SIGNER_URL}/wallets`);

  let walletPayload;
  try {
    walletPayload = JSON.parse(walletsResponse.body);
  } catch (_) {
    fail(`Signer wallet precheck returned non-JSON response from ${SIGNER_URL}`);
  }

  const signerWallets = Array.isArray(walletPayload.wallets) ? walletPayload.wallets : [];
  if (signerWallets.length < VUS) {
    fail(`Signer wallet precheck returned ${signerWallets.length} wallets but VUS=${VUS}`);
  }

  const baseRequiredBalance = estimateRequiredWalletBalance();
  const reserveUnits = parseInt(walletPayload.turnstile_min_sbc_units || TURNSTILE_MIN_SBC_UNITS);
  const selectedWallets = signerWallets
    .slice()
    .sort((a, b) => (a.wallet_index || 0) - (b.wallet_index || 0))
    .slice(0, VUS);

  for (const wallet of selectedWallets) {
    const requiredBalance = baseRequiredBalance + (wallet.turnstile_reserve_required ? reserveUnits : 0);
    if ((wallet.sbc_balance || 0) < requiredBalance) {
      fail(
        `Wallet ${wallet.wallet_index} (${wallet.address}) has SBC balance ${wallet.sbc_balance || 0}, ` +
        `below required pre-run minimum ${requiredBalance}. Top up loadgen wallets before running chaos load.`
      );
    }
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

  let shorten;
  for (let attempt = 1; attempt <= SHORTEN_RETRY_ATTEMPTS; attempt += 1) {
    shorten = http.post(
      `${BASE_URL}/shorten`,
      JSON.stringify(payload),
      { headers: { 'Content-Type': 'application/json' } },
    );

    if (shorten.status === 201 || !shouldRetryShorten(shorten) || attempt === SHORTEN_RETRY_ATTEMPTS) {
      break;
    }

    console.warn(
      `VU${__VU} shorten retry ${attempt}/${SHORTEN_RETRY_ATTEMPTS - 1}: ` +
      `tx_hash=${txHash || 'none'} status=${shorten.status} body=${shorten.body}`
    );
    sleep(SHORTEN_RETRY_DELAY_MS / 1000);
  }

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
