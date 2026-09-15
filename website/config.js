// claim.tpro.network runtime configuration - v3 (snapshot + merkle claim).
// This file is the ONLY deploy-time surface of the page: addresses, RPCs, the
// pinned merkle root + checksums and the location of the published table.
// Everything else is the inline app in index.html. Review the whole surface
// with two files. Run `node website/verify_deploy.mjs` before every push - it
// refuses a configuration whose pins do not match the table it serves.
// SECURITY: the page loads NO external scripts by policy - only this file and
// the inline app; a compromised CDN could otherwise swap the contract address.
window.CLAIM_CONFIG = {
  // true  = an OFFICIAL deployment on officialHost (preview before launch, live after).
  // false = a rehearsal / testnet / local copy: sticky "TEST COPY" banner, never trust it
  //         with mainnet tokens. With LIVE=true the page REQUIRES the three pins below
  //         (root, csvSha256, sha256sumsSha256) and kills itself when any is missing.
  LIVE: false,

  // The one official hostname. With LIVE=true the page warns (sticky) when served from
  // anywhere else (catches accidental mirrors / preview deploys; a deliberate clone is
  // out of any page's reach - hence the "official page only" copy and the wallet-prompt
  // description the page shows before every claim).
  officialHost: "claim.tpro.network",

  // Destination chain and contracts.
  base: {
    key: "base",
    name: "Base",
    chainId: 8453,
    chainIdHex: "0x2105",
    // Public read RPCs - ALL reads go here, never through the wallet provider (a malicious
    // wallet must not be able to spoof the root or the status). A LIST: reads fail over on
    // error, and the contract's merkle root must be confirmed by TWO providers before a
    // disagreement kills the page. Every host here MUST also be in the CSP connect-src of
    // index.html (meta tag) and vercel.json, or the browser blocks it silently.
    rpc: ["https://mainnet.base.org", "https://base.drpc.org", "https://1rpc.io/base"],
    explorer: "https://basescan.org",
    // TPRO on Base (the new token). Cross-checked live against distributor.token().
    token: "0x0000000000000000000000000000000000000000",
    // TPROMerkleDistributor on Base. ALL ZEROS = PREVIEW MODE: the page serves the
    // table for review (lookup + local proof check) and claims are off.
    distributor: "0x0000000000000000000000000000000000000000",
    // Block the distributor was deployed in (for explorers / future log scans).
    deployBlock: 0,
    // The distributor's sunsetTime() (36 months after launch), pinned at the T0 flip like the
    // root and the genesis; 0 = not pinned yet (verify_deploy.mjs compares the chain value on mainnet).
    sunsetTime: 0,
    // Sent to wallet_addEthereumChain when the wallet does not know Base yet.
    addChainParams: {
      chainId: "0x2105",
      chainName: "Base",
      nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
      rpcUrls: ["https://mainnet.base.org"],
      blockExplorerUrls: ["https://basescan.org"],
    },
  },

  // Contract-wallet designation path (decision 6): the address on ETHEREUM that a Safe /
  // smart-account holder sends the designation transaction to (the Base address in the
  // calldata). All zeros = not published yet (the marker still shows, without the address).
  designation: {
    chain: "Ethereum",
    address: "0x0000000000000000000000000000000000000000",
  },

  // The published snapshot table (docs/v3-interface.md section 2), served next to this
  // page: index.json, snapshot-<eth>-<poly>.csv, summary.json, contract-wallets.json,
  // proofs/<xx>.json (256 shards by the first two hex chars of the address), SHA256SUMS.
  // Relative URL = same origin as the page.
  table: {
    url: "./table/",
    // THREE PINS, copied from the snapshot tool's final lines at publication time:
    //   merkle root   - must equal index.json.merkleRoot and distributor.merkleRoot()
    //   csvSha256     - must equal index.json.csvSha256 and sha256sum of the served CSV
    //   sha256sumsSha256 - sha256sum of the served SHA256SUMS file, which in turn lists every
    //                   table file; with it the page can prove "your address is NOT in the
    //                   published table" instead of trusting whatever shard the host served.
    // ALL ZEROS / empty = not pinned: allowed only when LIVE is false (the page then warns
    // and marks absence answers as unverified).
    merkleRoot: "0x0000000000000000000000000000000000000000000000000000000000000000",
    csvSha256: "",
    sha256sumsSha256: "",
    // The two pinned snapshot blocks (informational, shown in STATUS and
    // cross-checked against index.json when non-zero).
    ethBlock: 0,
    polygonBlock: 0,
  },

  // Links shown in the footer / FAQ. Empty string = link hidden.
  links: {
    toolRepo: "",        // the PUBLIC repository of the snapshot tool, the contracts and this page
    announcement: "",    // the official announcement with the root + sha256
    community: "",       // the official community channel for the manual paths
    corrections: "",     // the public corrections list (post-T0 policy), if any
  },
};
