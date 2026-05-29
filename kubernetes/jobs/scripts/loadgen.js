import http from 'k6/http';
import encoding from 'k6/encoding';
import { check, sleep } from 'k6';
import { Rate } from 'k6/metrics';

// ── Custom metrics ────────────────────────────────────────────────────────────
// Post-x402: the signer no longer submits chain txs. The hot path is two HTTP
// hops (signer → app → facilitator). Metric set tracks each hop independently
// so a chaos run shows WHICH leg degraded.
const signSuccessRate     = new Rate('sign_success_rate');     // did /sign-permit2 return 200?
const paymentSettledRate  = new Rate('payment_settled_rate');  // did /shorten return 201 with PAYMENT-RESPONSE?
const shorten201Rate      = new Rate('shorten_201_rate');
const shorten402Rate      = new Rate('shorten_402_rate');
const shorten409Rate      = new Rate('shorten_409_rate');
const shorten5xxRate      = new Rate('shorten_5xx_rate');
const redirectOkRate      = new Rate('redirect_ok_rate');

// ── ENV vars ──────────────────────────────────────────────────────────────────
const BASE_URL          = __ENV.BASE_URL    || 'http://url-shortener-staging.url-shortener-staging.svc.cluster.local:80';
const SIGNER_URL        = __ENV.SIGNER_URL  || 'http://radius-signer.url-shortener-staging.svc.cluster.local:8080';
const NETWORK_CAIP2     = __ENV.NETWORK_CAIP2 || 'eip155:72344';
const SERVICE_WALLET    = __ENV.SERVICE_WALLET_ADDRESS;
const SBC_CONTRACT      = __ENV.SBC_CONTRACT_ADDRESS || '0x33ad9e4bd16b69b5bfded37d8b5d9ff9aba014fb';
const SBC_AMOUNT        = parseInt(__ENV.SBC_AMOUNT || '1000'); // 0.001 SBC (6 decimals)
const PAYMENT_ENABLED   = (__ENV.PAYMENT_ENABLED || 'true') === 'true';
const VUS               = parseInt(__ENV.VUS || '3');
const LOAD_PROFILE      = __ENV.LOAD_PROFILE || 'arrival-rate';
const ARRIVAL_RATE      = parseInt(__ENV.ARRIVAL_RATE || '60');
const ARRIVAL_TIME_UNIT = __ENV.ARRIVAL_TIME_UNIT || '1m';
const SLEEP_SECONDS     = parseFloat(__ENV.SLEEP_SECONDS || '2');
const PRECHECK_TIMEOUT  = __ENV.PRECHECK_TIMEOUT || '5s';
const DURATION          = __ENV.DURATION || '5m';
const PAYMENT_BUFFER    = parseInt(__ENV.PRECHECK_BALANCE_BUFFER_PAYMENTS || '5');

const thresholds = {
  // /sign-permit2 is pure CPU — sub-millisecond. Was a real chain submission in
  // the pre-x402 flow (~1.5s on Radius testnet); the threshold tightens by 25x.
  'http_req_duration{endpoint:sign}':     ['p(95)<100'],
  // /shorten now includes the facilitator's atomic on-chain Permit2.settle —
  // the pre-x402 budget (p95<400ms, when /shorten only verified a client-side
  // tx) is no longer realistic. Baseline observed in M6 2026-05-29: p95~680ms,
  // bottoming at ~540ms (Radius testnet single-tx finalization). 1000ms gives
  // comfortable headroom for chaos-induced variance.
  'http_req_duration{endpoint:shorten}':  ['p(95)<1000'],
  'http_req_duration{endpoint:redirect}': ['p(95)<100'],
  http_req_failed:   ['rate<0.05'],
  shorten_201_rate:  ['rate>0.90'],
  redirect_ok_rate:  ['rate>0.95'],
};

if (PAYMENT_ENABLED) {
  thresholds.sign_success_rate    = ['rate>0.99'];
  thresholds.payment_settled_rate = ['rate>0.95'];
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
          // maxVUs == VUS so each VU maps to one signer wallet — important
          // even under Permit2 (random nonces) because the signer's per-wallet
          // boot-time approval is what gates which wallets can sign.
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
  if (!match) fail(`Unsupported duration format: ${value}`);
  const multipliers = { ms: 1, s: 1000, m: 60000, h: 3600000 };
  return parseFloat(match[1]) * multipliers[match[2]];
}

function estimateRequiredWalletBalance() {
  // SBC moves payer → merchant on each /shorten. Permit2 approval was already
  // paid (one-time per wallet at signer boot), so no Turnstile reserve needed.
  const durationMs = parseDurationMs(DURATION);
  let expectedPaymentsPerWallet;
  if (LOAD_PROFILE === 'arrival-rate') {
    const totalPayments = Math.ceil((durationMs / parseDurationMs(ARRIVAL_TIME_UNIT)) * ARRIVAL_RATE);
    expectedPaymentsPerWallet = Math.max(1, Math.ceil(totalPayments / VUS));
  } else {
    const cycleSeconds = Math.max(SLEEP_SECONDS, 0.1);
    expectedPaymentsPerWallet = Math.max(1, Math.ceil(durationMs / (cycleSeconds * 1000)));
  }
  return (expectedPaymentsPerWallet + PAYMENT_BUFFER) * SBC_AMOUNT;
}

export function setup() {
  const health = http.get(`${BASE_URL}/health`, { timeout: PRECHECK_TIMEOUT });
  validateHttpResponse(health, `BASE_URL health check failed for ${BASE_URL}/health`);

  if (!PAYMENT_ENABLED) return;

  if (!SERVICE_WALLET) fail('SERVICE_WALLET_ADDRESS is required when PAYMENT_ENABLED=true');

  const signerHealth = http.get(`${SIGNER_URL}/health`, { timeout: PRECHECK_TIMEOUT });
  validateHttpResponse(signerHealth, `SIGNER_URL health check failed for ${SIGNER_URL}/health`);
  const signerHealthBody = JSON.parse(signerHealth.body);
  if ((signerHealthBody.wallet_count || 0) < VUS) {
    fail(`Signer has ${signerHealthBody.wallet_count || 0} wallets but VUS=${VUS}`);
  }
  if ((signerHealthBody.wallets_bootstrapped || 0) < VUS) {
    fail(
      `Signer reports ${signerHealthBody.wallets_bootstrapped || 0}/${signerHealthBody.wallet_count} ` +
      `wallets bootstrapped; need ${VUS}. Restart signer pod so Permit2 approvals retry.`
    );
  }

  const walletsResp = http.get(`${SIGNER_URL}/wallets`, { timeout: PRECHECK_TIMEOUT });
  validateHttpResponse(walletsResp, `SIGNER_URL wallet precheck failed for ${SIGNER_URL}/wallets`);
  const walletPayload = JSON.parse(walletsResp.body);
  const signerWallets = Array.isArray(walletPayload.wallets) ? walletPayload.wallets : [];
  if (signerWallets.length < VUS) {
    fail(`Signer returned ${signerWallets.length} wallets but VUS=${VUS}`);
  }

  const required = estimateRequiredWalletBalance();
  const selected = signerWallets
    .slice()
    .sort((a, b) => (a.wallet_index || 0) - (b.wallet_index || 0))
    .slice(0, VUS);

  for (const w of selected) {
    if (!w.bootstrapped) {
      fail(`Wallet ${w.wallet_index} (${w.address}) is not bootstrapped; restart signer pod`);
    }
    if ((w.permit2_allowance || 0) <= 0) {
      fail(`Wallet ${w.wallet_index} (${w.address}) has no Permit2 allowance; restart signer pod`);
    }
    if ((w.sbc_balance || 0) < required) {
      fail(
        `Wallet ${w.wallet_index} (${w.address}) has SBC balance ${w.sbc_balance || 0}, ` +
        `below required ${required} for this run. Top up loadgen wallets first.`
      );
    }
  }
}

function buildPaymentSignatureHeader(signerResponse) {
  const envelope = {
    x402Version: 2,
    accepted: {
      scheme: 'exact',
      network: NETWORK_CAIP2,
      amount: String(SBC_AMOUNT),
      asset: SBC_CONTRACT,
      payTo: SERVICE_WALLET,
      maxTimeoutSeconds: 300,
      extra: { assetTransferMethod: 'permit2', name: 'Stable Coin', version: '1' },
    },
    payload: {
      signature: signerResponse.signature,
      permit2Authorization: signerResponse.permit2Authorization,
    },
  };
  return encoding.b64encode(JSON.stringify(envelope));
}

// ── Main loop ─────────────────────────────────────────────────────────────────
export default function () {
  let paymentSignature = null;

  if (PAYMENT_ENABLED) {
    // Step 1: ask the signer for a Permit2 authorization. Pure CPU — no RPC.
    const signResp = http.post(
      `${SIGNER_URL}/sign-permit2`,
      JSON.stringify({ wallet_index: __VU - 1, amount: SBC_AMOUNT }),
      {
        headers: { 'Content-Type': 'application/json' },
        timeout: '5s',
        tags: { endpoint: 'sign' },
      },
    );

    if (signResp.status !== 200) {
      console.error(`VU${__VU} /sign-permit2 failed: status=${signResp.status} body=${signResp.body}`);
      signSuccessRate.add(false);
      paymentSettledRate.add(false);
      return;
    }

    let signBody;
    try {
      signBody = JSON.parse(signResp.body);
    } catch (e) {
      console.error(`VU${__VU} signer returned invalid JSON: ${e}`);
      signSuccessRate.add(false);
      paymentSettledRate.add(false);
      return;
    }

    if (!signBody.signature || !signBody.permit2Authorization) {
      console.error(`VU${__VU} signer response missing fields`);
      signSuccessRate.add(false);
      paymentSettledRate.add(false);
      return;
    }

    signSuccessRate.add(true);
    paymentSignature = buildPaymentSignatureHeader(signBody);
  }

  // Step 2: POST /shorten with PAYMENT-SIGNATURE header (no more tx_hash body field).
  const headers = { 'Content-Type': 'application/json' };
  if (paymentSignature) headers['PAYMENT-SIGNATURE'] = paymentSignature;

  const shorten = http.post(
    `${BASE_URL}/shorten`,
    JSON.stringify({ url: `https://example.com/load-test-${__VU}-${Date.now()}` }),
    { headers, tags: { endpoint: 'shorten' } },
  );

  check(shorten, { 'shorten 201': (r) => r.status === 201 });
  shorten201Rate.add(shorten.status === 201);
  shorten402Rate.add(shorten.status === 402);
  shorten409Rate.add(shorten.status === 409);
  shorten5xxRate.add(shorten.status >= 500 && shorten.status < 600);

  if (PAYMENT_ENABLED) {
    // payment_settled_rate is the cleanest end-to-end "did the pay flow work"
    // signal: 201 from /shorten AND the facilitator gave us a tx hash (echoed
    // in PAYMENT-RESPONSE). Skips the existing-URL 200 case which doesn't pay.
    const settled = shorten.status === 201 && Boolean(shorten.headers['Payment-Response']);
    paymentSettledRate.add(settled);
  }

  if (shorten.status !== 201 && shorten.status !== 200) {
    console.error(`VU${__VU} shorten failed: status=${shorten.status} body=${shorten.body}`);
  }
  if (shorten.status === 409) {
    console.warn(`VU${__VU} 409 — duplicate settlement_tx_hash (Permit2 replay caught at app layer)`);
  }

  // Step 3: GET /{code} — verify redirect (no follow).
  if (shorten.status === 201) {
    let code = '';
    try { code = JSON.parse(shorten.body).code || ''; } catch (_) {}
    if (code) {
      const redirect = http.get(`${BASE_URL}/${code}`, {
        redirects: 0,
        tags: { endpoint: 'redirect' },
      });
      check(redirect, { 'redirect 302': (r) => r.status === 302 });
      redirectOkRate.add(redirect.status === 302);
    }
  }

  if (LOAD_PROFILE === 'closed-loop') sleep(SLEEP_SECONDS);
}
