#!/usr/bin/env python3
# Copyright (c) 2015-2017 The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Test block processing.

This reimplements tests from the bitcoinj/FullBlockTestGenerator used
by the pull-tester.

We use the testing framework in which we expect a particular answer from
each test.
"""
from io import BytesIO

from test_framework.test_framework import ComparisonTestFramework
from test_framework.util import *
from test_framework.comptool import TestManager, TestInstance, RejectResult
from test_framework.blocktools import *
import time
from test_framework.key import CECKey
from test_framework.script import *
from test_framework.mininode import network_thread_start
import struct


class PreviousSpendableOutput():
    def __init__(self, tx=CTransaction(), n=-1):
        self.tx = tx
        self.n = n  # the output we're spending

#  Use this class for tests that require behavior other than normal "mininode" behavior.
#  For now, it is used to serialize a bloated varint (b64).


class CBrokenBlock(CBlock):
    def __init__(self, header=None):
        super(CBrokenBlock, self).__init__(header)

    def initialize(self, base_block):
        self.vtx = copy.deepcopy(base_block.vtx)
        self.hashMerkleRoot = self.calc_merkle_root()

    def serialize(self, with_witness=False):
        r = b""
        r += super(CBlock, self).serialize()
        r += struct.pack("<BQ", 255, len(self.vtx))
        for tx in self.vtx:
            if with_witness:
                r += tx.serialize_with_witness()
            else:
                r += tx.serialize_without_witness()
        r += ser_string(self.vchBlockSig)
        return r

    def normal_serialize(self):
        r = b""
        r += super(CBrokenBlock, self).serialize()
        return r


fee = min_fee


class FullBlockTest(ComparisonTestFramework):
    # Can either run this test as 1 node with expected answers, or two and compare them.
    # Change the "outcome" variable from each TestInstance object to only do the comparison.
    def set_test_params(self):
        self.num_nodes = 1
        self.setup_clean_chain = True
        self.block_heights = {}
        self.coinbase_key = CECKey()
        self.coinbase_key.set_secretbytes(b"horsebattery")
        self.coinbase_pubkey = self.coinbase_key.get_pubkey()
        self.tip = None
        self.blocks = {}

    def add_options(self, parser):
        super().add_options(parser)
        parser.add_option("--runbarelyexpensive",
                          dest="runbarelyexpensive", default=True)

    def run_test(self):
        self.test = TestManager(self, self.options.tmpdir)
        self.test.add_all_connections(self.nodes)
        network_thread_start()
        self.test.run()

    def add_transactions_to_block(self, block, tx_list):
        [tx.rehash() for tx in tx_list]
        block.vtx.extend(tx_list)

    # this is a little handier to use than the version in blocktools.py
    def create_tx(self, spend_tx, n, value, script=CScript([OP_TRUE, OP_DROP] * 15 + [OP_TRUE])):
        tx = create_transaction(spend_tx, n, b"", value, script)
        return tx

    # sign a transaction, using the key we know about
    # this signs input 0 in tx, which is assumed to be spending output n in spend_tx
    def sign_tx(self, tx, spend_tx, n):
        scriptPubKey = bytearray(spend_tx.vout[n].scriptPubKey)
        if (scriptPubKey[0] == OP_TRUE):  # an anyone-can-spend
            tx.vin[0].scriptSig = CScript()
            return
        (sighash, err) = SignatureHash(
            spend_tx.vout[n].scriptPubKey, tx, 0, SIGHASH_ALL)
        tx.vin[0].scriptSig = CScript(
            [self.coinbase_key.sign(sighash) + bytes(bytearray([SIGHASH_ALL]))])

    def create_and_sign_transaction(self, spend_tx, n, value, script=CScript([OP_TRUE])):
        tx = self.create_tx(spend_tx, n, value, script)
        self.sign_tx(tx, spend_tx, n)
        tx.rehash()
        return tx

    def next_block(self, block_label, spend=None, additional_coinbase_value=0, script=CScript([OP_TRUE]), solve=True):
        # useful marker for debugging, marks the last block that was created
        logger.info("Creating block: {}".format(block_label))
        if self.tip is None:
            base_block_hash = self.genesis_hash
            block_time = int(time.time()) + 1
        else:
            base_block_hash = self.tip.sha256
            block_time = self.tip.nTime + 1
        # First create the coinbase
        height = self.block_heights[base_block_hash] + 1
        coinbase = create_coinbase(height, self.coinbase_pubkey)
        coinbase.vout[0].nValue += additional_coinbase_value
        coinbase.rehash()
        if spend is None:
            block = create_block(base_block_hash, coinbase, block_time)
        else:
            coinbase.vout[0].nValue += fee  # add the fee to coinbase
            coinbase.rehash()
            block = create_block(base_block_hash, coinbase, block_time)
            # create a new transaction, but remove the fee from it
            amount_to_send = spend.tx.vout[spend.n].nValue - fee
            # spend 10000000 satoshi
            tx = create_transaction(
                spend.tx, spend.n, b"", amount_to_send, script)
            self.sign_tx(tx, spend.tx, spend.n)
            self.add_transactions_to_block(block, [tx])
            block.hashMerkleRoot = block.calc_merkle_root()

        # fix block time to never be younger than transactions
        block.fix_time_then_resolve(False)

        if solve:
            # add neblio signature
            block.solve()
            block.vchBlockSig = self.coinbase_key.sign(
                bytes.fromhex(block.hash)[::-1])
        else:
            block.rehash()
        logger.info("Created block number {} with hash {}".format(
            block_label, block.hash))
        self.tip = block
        self.block_heights[block.sha256] = height
        assert block_label not in self.blocks
        self.blocks[block_label] = block
        return block

    def format_hash_to_str(self, hashPrevBlock):
        return hex(hashPrevBlock).replace("0x", "").zfill(len(hex(self.genesis_hash).replace("0x", "")))

    def format_block_to_string(self, block, extra_label):
        truncate_len = 12
        return self.format_hash_to_str(block.sha256)[:truncate_len] + " " + str(self.block_heights[block.sha256]) + " " + str(extra_label)

    def format_prevblock_to_string(self, block):
        truncate_len = 12
        return self.format_hash_to_str(block.hashPrevBlock)[:truncate_len] + " " + str(self.block_heights[block.hashPrevBlock])

    def create_blockchain_graph(self):
        # pygraph can be obtained from https://github.com/Shoobx/python-graph
        import pygraph
        import pygraph.classes.graph
        gr = pygraph.classes.graph.graph()
        genesis_label = "genesis"
        gr.add_node(genesis_label)
        for b in self.blocks:
            # we fill becausse integer to hex omits leading zeros
            curr_hash = self.format_block_to_string(self.blocks[b], "b"+str(b))
            gr.add_node(curr_hash)
        for b in self.blocks:
            prev_hash = self.format_prevblock_to_string(self.blocks[b])
            curr_hash = self.format_block_to_string(self.blocks[b], "b"+str(b))
            prev_hash = genesis_label if self.blocks[b].hashPrevBlock == self.genesis_hash else prev_hash
            # find the prev node that contains the prev_hash
            prev_node = [label for label in gr.nodes() if prev_hash in label]
            # only one node with that hash should be found
            assert len(prev_node) == 1
            gr.add_edge((curr_hash, prev_node[0]))
        return gr

    @staticmethod
    def write_graph_to_file(gr, filename):
        from pygraph.readwrite import dot, markup
        dotstr = dot.write(gr)
        with open(filename, "w") as f:
            f.write(dotstr)

    def run_genesis_block_hash_test(self):
        """
        Test that hashing of a block works fine
        Returns: nothing
        """
        genesis_block_hex = self.nodes[0].getblock(
            self.nodes[0].getbestblockhash(), False)
        genesis_block = CBlock()
        genesis_block_raw_io = BytesIO(bytes.fromhex(genesis_block_hex))
        genesis_block.deserialize(genesis_block_raw_io)
        genesis_block.rehash()
        assert genesis_block.hash is not None
        assert_equal(genesis_block.hash, self.nodes[0].getblockhash(0))
        assert_equal(genesis_block.hash, self.nodes[0].calculateblockhash(
            genesis_block.serialize().hex()))
        assert_equal(genesis_block_hex, genesis_block.serialize().hex())

    def get_tests(self):
        self.genesis_hash = int(self.nodes[0].getbestblockhash(), 16)
        self.run_genesis_block_hash_test()
        self.block_heights[self.genesis_hash] = 0
        spendable_outputs = []

        # save the current tip so it can be spent by a later block
        def save_spendable_output():
            spendable_outputs.append(self.tip)

        # get an output that we previously marked as spendable
        def get_spendable_output(index=0):
            return PreviousSpendableOutput(spendable_outputs.pop(index).vtx[0], 0)

        # returns a test case that asserts that the current tip was accepted
        def accepted():
            return TestInstance([[self.tip, True]])

        # returns a test case that asserts that the current tip was rejected
        # it DOESN'T mean the block is invalid. It means it's (still) not the new tip
        def rejected(reject=None):
            if reject is None:
                return TestInstance([[self.tip, False]])
            else:
                return TestInstance([[self.tip, reject]])

        # move the tip back to a previous block
        def tip(number):
            self.tip = self.blocks[number]

        # adds transactions to the block and updates state
        def update_block(block_number, new_transactions, update_time=True):
            # useful marker for debugging, marks the last block that was created
            logger.info("Updating block: {}".format(block_number))
            block = self.blocks[block_number]
            self.add_transactions_to_block(block, new_transactions)
            old_sha256 = block.sha256
            block.hashMerkleRoot = block.calc_merkle_root()
            if update_time:
                block.fix_time_then_resolve(False)
            block.solve()
            block.vchBlockSig = self.coinbase_key.sign(
                bytes.fromhex(block.hash)[::-1])
            # Update the internal state just like in next_block
            self.tip = block
            if block.sha256 != old_sha256:
                self.block_heights[block.sha256] = self.block_heights[old_sha256]
                del self.block_heights[old_sha256]
            self.blocks[block_number] = block
            # useful marker for debugging, marks the last block that was created
            logger.info("Updated block {} with hash {}".format(
                block_number, block.hash))
            return block

        def create_tx_manual(prevout_hash, n, value):
            tx = CTransaction()
            tx.vin.append(CTxIn(COutPoint(prevout_hash, n), b'', 0xffffffff))
            tx.vout.append(CTxOut(value, CScript([OP_TRUE])))
            tx.calc_sha256()
            return tx

        def get_block_hash_str_from_label(label):
            return self.blocks[label].hash

        def is_block_mainchain_from_label(label):
            blk = self.nodes[0].getblock(get_block_hash_str_from_label(label))
            return blk['confirmations'] != -1

        def get_block_height_from_label(label):
            return self.block_heights[self.blocks[label].sha256]

        # shorthand for functions
        block = self.next_block
        create_tx = self.create_tx
        create_and_sign_tx = self.create_and_sign_transaction

        # disable caching
        self.nodes[0].setviupushprobability(0, 100)

        # Create a new block
        block(0)
        save_spendable_output()
        yield accepted()

        # Now we need that block to mature so we can spend the coinbase.
        test = TestInstance(sync_every_block=False)
        for i in range(399):
            block(5000 + i)
            test.blocks_and_transactions.append([self.tip, True])
            save_spendable_output()
        yield test

        # collect spendable outputs now to avoid cluttering the code later on
        out = []
        out_total_count = len(spendable_outputs)
        for i in range(out_total_count):
            out.append(get_spendable_output())

        blocks_until_fork_count = 15
        height_before_fork_work = self.nodes[0].getblockcount()

        def make_tx_custom(tx_to_spent_dict):
            tx = create_transaction(
                tx_to_spent_dict['tx'], tx_to_spent_dict['vout'], b"", 1 * COIN, CScript([OP_TRUE]))
            self.sign_tx(tx, tx_to_spent_dict['tx'], tx_to_spent_dict['vout'])
            return tx

        txs_before_old_fork = []
        tx_made_and_spant_before_forks = None
        tx_made_before_forks_and_spent_in_new_fork = None
        tx_made_at_the_new_fork_and_spent_in_the_new_fork = None
        # Start by building a couple of blocks on top
        for i in range(50):
            block_label = i+1
            if i < blocks_until_fork_count:
                block(block_label, spend=out[i])
                txs_before_old_fork.append({'tx': self.tip.vtx[1], 'vout': 0})
            else:
                block(block_label, spend=out[i])
                save_spendable_output()

            # the new fork starts at 10, old fork starts at 15, so here we're before both
            if i == 2:
                tx_made_and_spant_before_forks = txs_before_old_fork[0]
                tx = make_tx_custom(tx_made_and_spant_before_forks)
                update_block(block_label, [tx])

            # 10-14 is expected to be the new fork (between the old and new common ancestors)
            if i == 12:
                tx_made_before_forks_and_spent_in_new_fork = txs_before_old_fork[1]
                tx = make_tx_custom(tx_made_before_forks_and_spent_in_new_fork)
                update_block(block_label, [tx])

            if i == 12:
                tx_made_at_the_new_fork_and_spent_in_the_new_fork = txs_before_old_fork[11]
                tx = make_tx_custom(tx_made_at_the_new_fork_and_spent_in_the_new_fork)
                update_block(block_label, [tx])

            yield accepted()

        old_common_ancestor_height = get_block_height_from_label(15)
        assert_equal(old_common_ancestor_height, height_before_fork_work + blocks_until_fork_count)

        assert is_block_mainchain_from_label(15)

        # build a fork
        old_fork_tip = None
        tip(15)
        txs_in_old_fork = []
        tx_made_before_forks_and_spent_in_old_fork = None
        tx_made_at_the_new_fork_and_spent_in_the_old_fork = None
        tx_made_at_the_old_fork_and_spent_in_the_old_fork = None
        for i in range(10):
            block_label = "f_oldfork_{}".format(i+1)
            block(block_label.format(i), spend=out[blocks_until_fork_count+i])
            txs_in_old_fork.append({'tx': self.tip.vtx[1], 'vout': 0})
            if i == 4:  # spend in (4+1)th block, use from (1+1)th block in the array (see index below)
                tx_made_at_the_old_fork_and_spent_in_the_old_fork = txs_in_old_fork[1]
                tx = make_tx_custom(tx_made_at_the_old_fork_and_spent_in_the_old_fork)
                update_block(block_label, [tx])

            if i == 2:
                tx_made_before_forks_and_spent_in_old_fork = txs_before_old_fork[2]
                tx = make_tx_custom(tx_made_before_forks_and_spent_in_old_fork)
                update_block(block_label, [tx])

            if i == 3:
                tx_made_at_the_new_fork_and_spent_in_the_old_fork = txs_before_old_fork[13]
                tx = make_tx_custom(tx_made_at_the_new_fork_and_spent_in_the_old_fork)
                update_block(block_label, [tx])


            if i == 3:
                # enable cache at one arbitrary block
                self.nodes[0].setviupushprobability(100, 100)
            else:
                self.nodes[0].setviupushprobability(0, 100)

            # save_spendable_output()
            yield rejected()
            assert not is_block_mainchain_from_label(block_label)
            assert_equal(get_block_height_from_label(block_label), old_common_ancestor_height + 1 + i)
            tip(block_label)
            old_fork_tip = block_label

        self.nodes[0].setviupushprobability(0, 100)

        blocks_until_new_common_ancestor = 10

        new_common_ancestor_height = get_block_height_from_label(10)
        assert_equal(new_common_ancestor_height, height_before_fork_work + blocks_until_new_common_ancestor)

        # disable caching again
        self.nodes[0].setviupushprobability(0, 100)
        # build a fork that pushes more into the fork
        tip(10)
        for i in range(60):
            block_label = "f_newfork_{}".format(i+1)
            block(block_label.format(i), spend=out[10+i])
            if i < 40:
                # until the chain is longer
                yield rejected()
            else:
                yield accepted()
            assert_equal(get_block_height_from_label(block_label), new_common_ancestor_height + 1 + i)
            tip(block_label)

        for i in range(60):
            block_label = "f_newfork_{}".format(i+1)
            assert is_block_mainchain_from_label(block_label)

        # ### now we test that double-spending is not possible
        # Try to spend from something already spent before the fork, but made before the forks
        tip(old_fork_tip)
        height = self.block_heights[self.blocks[old_fork_tip].sha256]
        block_label = "f_ds_1_{}".format(height)
        block(block_label.format(height))
        tx = make_tx_custom(tx_made_and_spant_before_forks)
        update_block(block_label, [tx])
        yield rejected(RejectResult(16, b'bad-txns-inputs-missingorspent-DoublespendAttempt_SpentAlreadyBeforeTheFork'))

        # Try to spend from something already spent in the new fork, but made before the forks
        tip(old_fork_tip)
        height = self.block_heights[self.blocks[old_fork_tip].sha256]
        block_label = "f_ds_2_{}".format(height)
        block(block_label.format(height))
        tx = make_tx_custom(tx_made_before_forks_and_spent_in_new_fork)
        update_block(block_label, [tx])
        yield rejected(RejectResult(16, b'bad-txns-inputs-missingorspent-DoublespendAttempt_WithinTheFork'))

        # Try to spend from something already spent in the old fork, but made before the forks
        tip(old_fork_tip)
        height = self.block_heights[self.blocks[old_fork_tip].sha256]
        block_label = "f_ds_3_{}".format(height)
        block(block_label.format(height))
        tx = make_tx_custom(tx_made_before_forks_and_spent_in_old_fork)
        update_block(block_label, [tx])
        yield rejected(RejectResult(16, b'bad-txns-inputs-missingorspent-DoublespendAttempt_WithinTheFork'))

        # Try to spend from something already spent in the new fork but made in the new fork
        tip(old_fork_tip)
        height = self.block_heights[self.blocks[old_fork_tip].sha256]
        block_label = "f_ds_4_{}".format(height)
        block(block_label.format(height))
        tx = make_tx_custom(tx_made_at_the_new_fork_and_spent_in_the_new_fork)
        update_block(block_label, [tx])
        yield rejected(RejectResult(16, b'bad-txns-inputs-missingorspent-DoublespendAttempt_WithinTheFork'))

        # Try to spend from something already spent in the old fork but made in the new fork
        tip(old_fork_tip)
        height = self.block_heights[self.blocks[old_fork_tip].sha256]
        block_label = "f_ds_5_{}".format(height)
        block(block_label.format(height))
        tx = make_tx_custom(tx_made_at_the_new_fork_and_spent_in_the_old_fork)
        update_block(block_label, [tx])
        yield rejected(RejectResult(16, b'bad-txns-inputs-missingorspent-DoublespendAttempt_WithinTheFork'))

        # Try to spend from something already spent in the old fork but made in the old fork
        tip(old_fork_tip)
        height = self.block_heights[self.blocks[old_fork_tip].sha256]
        block_label = "f_ds_6_{}".format(height)
        block(block_label.format(height))
        tx = make_tx_custom(tx_made_at_the_old_fork_and_spent_in_the_old_fork)
        update_block(block_label, [tx])
        yield rejected(RejectResult(16, b'bad-txns-inputs-missingorspent-DoublespendAttempt_WithinTheFork'))


if __name__ == '__main__':
    FullBlockTest().main()
