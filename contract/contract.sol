// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * @title EnergyAuditHash
 * @dev ΤΕΛΙΚΗ ΕΚΔΟΣΗ (v3) - Βασισμένη στην ιδέα του Hashing!
 *
 * ΑΥΤΟ ΤΟ ΣΥΜΒΟΛΑΙΟ ΔΕΝ ΑΠΟΘΗΚΕΥΕΙ ΔΕΔΟΜΕΝΑ.
 * Αποθηκεύει ΜΟΝΟ ένα "hash" (ένα cryptographic fingerprint)
 * των δεδομένων της συναλλαγής για μέγιστη προστασία ιδιωτικότητας (privacy).
 *
 * Η ροή είναι:
 * 1. Backend (Python): Κάνει τη συναλλαγή στη SQL DB (π.χ. tradeId: 42).
 * 2. Backend: Φτιάχνει ένα JSON/string με τα data (π.χ. "{'id':42, 'kwh':10.5, ...}")
 * 3. Backend: Κάνει hash (π.χ. SHA-256) αυτό το string -> 0xabc123...
 * 4. Backend -> Frontend: Δίνει το tradeId (42) και το hash (0xabc123...).
 * 5. Frontend -> MetaMask -> Καλεί τη συνάρτηση `logTradeHash(42, 0xabc123...)`
 * 6. MetaMask -> Frontend: Επιστρέφει το tx_hash.
 * 7. Frontend -> Backend: Καλεί το `/chain/trade-confirm` με το tx_hash.
 */
contract EnergyAuditHash
{

    address public gridOperator;

    /**
     * @dev "Φωνάζει" το hash της συναλλαγής.
     * Το on-chain event περιέχει ΜΟΝΟ το ID και το hash.
     * Όλα τα άλλα data (kwh, eur) μένουν με ασφάλεια στο backend (off-chain).
     */
    event TradeHashLogged(
        uint256 indexed tradeId,      // Το ID από τον SQL πίνακα 'Trade'
        bytes32 indexed dataHash      // Το SHA-256 hash του 
JSON/string
    );

    constructor()
    {
        gridOperator = msg.sender;
    }

    /**
     * @dev (Για το Frontend) Καλείται ΑΦΟΥ η συναλλαγή γίνει στο backend.
     * Ο αγοραστής καλεί αυτή τη συνάρτηση για να "σφραγίσει" το hash
     * της συναλλαγής του στο blockchain.
     *
     * @param _tradeId Το ID της συναλλαγής από τη βάση δεδομένων (π.χ. 42)
     * @param _dataHash Το hash (bytes32) των δεδομένων της συναλλαγής.
     */
    function logTradeHash
    (
        uint256 _tradeId,
        bytes32 _dataHash
    ) public
    {
        // Απλά εκπέμπουμε το Event.
        // Αυτή είναι η αδιάφθορη, on-chain "απόδειξη"
        // ότι μια συναλλαγή με αυτό το hash όντως συνέβη.
        emit TradeHashLogged
        (
            _tradeId,
            _dataHash
        );
    }
}