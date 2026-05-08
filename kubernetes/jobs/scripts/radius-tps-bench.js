import { Client } from 'k6/x/ethereum';
import { Trend, Counter } from 'k6/metrics';

// ── Custom metrics ────────────────────────────────────────────────────────────
// true = native histogram → k6 auto-computes p50/p90/p95/p99 for Grafana
const confirmLatency = new Trend('radius_confirm_ms', true);
const txTotal        = new Counter('radius_tx_total');

// ── ENV vars ──────────────────────────────────────────────────────────────────
const RPC_URL        = __ENV.RPC_URL             || 'https://rpc.testnet.radiustech.xyz';
const CHAIN_ID       = parseInt(__ENV.CHAIN_ID   || '72344');
const SERVICE_WALLET = __ENV.SERVICE_WALLET_ADDRESS;
const WALLET_KEY     = __ENV.WALLET_KEY_1; // single wallet, single nonce sequence
const SBC_CONTRACT   = '0x33ad9e4bd16b69b5bfded37d8b5d9ff9aba014fb';
const SBC_AMOUNT     = 1000; // 0.001 SBC (6 decimals)
const TX_COUNT       = parseInt(__ENV.TX_COUNT || '100');

// ── Options ───────────────────────────────────────────────────────────────────
// VUS=1 + iterations=1 → runs the default function exactly once.
// All 100 transactions happen inside that single call — gives us
// a burst pattern from one wallet with no nonce conflicts.
export const options = {
  vus:        1,
  iterations: 1,
};

// ── Per-VU init ───────────────────────────────────────────────────────────────
if (!WALLET_KEY) {
  throw new Error('WALLET_KEY_1 is not set');
}

const client = new Client({
  url:        RPC_URL,
  privateKey: WALLET_KEY, // no 0x prefix
});

// ── ABI encode ERC-20 transfer(address,uint256) ───────────────────────────────
function encodeERC20Transfer(to, amount) {
  const selector  = 'a9059cbb';
  const paddedTo  = to.replace('0x', '').toLowerCase().padStart(64, '0');
  const paddedAmt = parseInt(amount).toString(16).padStart(64, '0');
  return '0x' + selector + paddedTo + paddedAmt;
}

// ── Benchmark ─────────────────────────────────────────────────────────────────
// Sends TX_COUNT transactions as fast as possible (no sleep).
// Each transaction is sent and confirmed sequentially — xk6-ethereum manages
// the nonce internally. Confirmation latency per tx is recorded in the
// radius_confirm_ms Trend metric (visible in Grafana after the run).
//
// Why sequential and not true parallel bursts:
// xk6-ethereum's sendRawTransaction fetches the pending nonce from the chain
// before signing each tx. On Radius, sub-second finality means pending nonce
// updates almost instantly, so sequential sends with immediate receipt-wait
// work reliably. A parallel burst (send N → wait N) risks nonce conflicts if
// the chain hasn't processed earlier txs before the next nonce query.
export default function () {
  if (!SERVICE_WALLET) {
    console.error('SERVICE_WALLET_ADDRESS is required');
    return;
  }

  const data       = encodeERC20Transfer(SERVICE_WALLET, SBC_AMOUNT);
  const benchStart = Date.now();

  console.log(`Starting Radius TPS benchmark: ${TX_COUNT} transactions`);
  console.log(`RPC: ${RPC_URL}  Chain: ${CHAIN_ID}  Contract: ${SBC_CONTRACT}`);

  for (let i = 0; i < TX_COUNT; i++) {
    const txStart = Date.now();

    let txHash;
    try {
      txHash = client.sendRawTransaction({
        to:   SBC_CONTRACT,
        gas:  100000,
        chain_id: CHAIN_ID,
        input: data,
      });
      client.waitForTransactionReceipt(txHash, 30);
    } catch (e) {
      console.error(`tx ${i + 1} failed: ${e}`);
      continue;
    }

    const latency = Date.now() - txStart;
    confirmLatency.add(latency);
    txTotal.add(1);

    // Log every 10th tx so the job output is readable without being spammy
    if ((i + 1) % 10 === 0) {
      console.log(`  ${i + 1}/${TX_COUNT} confirmed — this tx: ${latency}ms`);
    }
  }

  const totalMs = Date.now() - benchStart;
  const tpm     = Math.round((TX_COUNT / totalMs) * 60000);

  console.log(`\n=== Radius TPS Benchmark Results ===`);
  console.log(`Total txs  : ${TX_COUNT}`);
  console.log(`Total time : ${(totalMs / 1000).toFixed(1)}s`);
  console.log(`Throughput : ~${tpm} tx/min`);
  console.log(`See Grafana → radius_confirm_ms for p50/p95/p99 confirmation latency`);
  console.log(`\nNote: eth_sendRawTransactionSync (EIP-7966) cuts this latency ~50% further.`);
  console.log(`Demo: cast mktx + curl eth_sendRawTransactionSync — see CLAUDE.md for the command.`);
}
