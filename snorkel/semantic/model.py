# Semparser
from semparse_examples import get_examples
from load_external_annotations import load_external_labels
from utils import TaggerOneTagger

# Snorkel
from snorkel.models import Document, Sentence, candidate_subclass
from snorkel.parser import CorpusParser, TSVDocPreprocessor, XMLMultiDocPreprocessor
from snorkel.candidates import Ngrams, CandidateExtractor, PretaggedCandidateExtractor
from snorkel.matchers import PersonMatcher
from snorkel.annotations import (FeatureAnnotator, LabelAnnotator, 
    save_marginals, load_marginals, load_gold_labels)
from snorkel.learning import GenerativeModel, SparseLogisticRegression
from snorkel.learning import RandomSearch, ListParameter, RangeParameter
from snorkel.learning.utils import MentionScorer, training_set_summary_stats
from snorkel.learning.structure import DependencySelector
from snorkel.semantic import SemanticParser

# Python
import numpy as np
import matplotlib.pyplot as plt
import os
import random
import csv
import cPickle
import bz2
from pprint import pprint
from scipy.sparse import coo_matrix

TRAIN = 0
DEV = 1
TEST = 2

class SnorkelModel(object):
    """
    A class for running a complete Snorkel pipeline
    """
    def __init__(self, session, candidate_class, config):
        self.session = session
        self.candidate_class = candidate_class
        self.config = config

        if config['seed']:
            np.random.seed(config['seed'])

        self.LFs = None
        self.labeler = None
        self.featurizer = None

    def parse(self, doc_preprocessor, fn=None, clear=True):
        corpus_parser = CorpusParser(fn=fn)
        corpus_parser.apply(doc_preprocessor, count=doc_preprocessor.max_docs, 
                            parallelism=self.config['parallelism'], clear=clear)

    def extract(self, cand_extractor, sents, split, clear=True):
        cand_extractor.apply(sents, split=split, parallelism=self.config['parallelism'], clear=clear)

    def load_gold(self):
        raise NotImplementedError

    def featurize(self, featurizer, split):
        if split == TRAIN:
            F = featurizer.apply(split=split, parallelism=self.config['parallelism'])
        else:
            F = featurizer.apply_existing(split=split, parallelism=self.config['parallelism'])
        return F

    def label(self, labeler, split):
        if split == TRAIN:
            L = labeler.apply(split=split, parallelism=self.config['parallelism'])
        else:
            L = labeler.apply_existing(split=split, parallelism=self.config['parallelism'])
        return L

    def generative(self):
        raise NotImplementedError
    
    def discriminative(self):
        raise NotImplementedError

class CDRModel(SnorkelModel):
    """
    A class specifically intended for use with the CDR task/tutorial/dataset
    """
    def parse(self, file_path=(os.environ['SNORKELHOME'] + '/tutorials/cdr/data/CDR.BioC.xml'), clear=True):
        doc_preprocessor = XMLMultiDocPreprocessor(
            path=file_path,
            doc='.//document',
            text='.//passage/text/text()',
            id='.//id/text()',
            max_docs=self.config['max_docs']
        )
        tagger_one = TaggerOneTagger()
        fn=tagger_one.tag
        SnorkelModel.parse(self, doc_preprocessor, fn=fn, clear=clear)
        if self.config['verbose']:
            print("Documents: {}".format(self.session.query(Document).count()))
            print("Sentences: {}".format(self.session.query(Sentence).count()))

    def extract(self, clear=True):
        with open(os.environ['SNORKELHOME'] + '/tutorials/cdr/data/doc_ids.pkl', 'rb') as f:
            train_ids, dev_ids, test_ids = cPickle.load(f)
        train_ids, dev_ids, test_ids = set(train_ids), set(dev_ids), set(test_ids)

        train_sents, dev_sents, test_sents = set(), set(), set()
        docs = self.session.query(Document).order_by(Document.name).all()
        for i, doc in enumerate(docs):
            for s in doc.sentences:
                if doc.name in train_ids:
                    train_sents.add(s)
                elif doc.name in dev_ids:
                    dev_sents.add(s)
                elif doc.name in test_ids:
                    test_sents.add(s)
                else:
                    raise Exception('ID <{0}> not found in any id set'.format(doc.name))

        candidate_extractor = PretaggedCandidateExtractor(self.candidate_class, ['Chemical', 'Disease'])
        for split, sents in enumerate([train_sents, dev_sents, test_sents]):
            if len(sents) > 0 and split in self.config['splits']:
                SnorkelModel.extract(self, candidate_extractor, sents, split=split, clear=clear)
                nCandidates = self.session.query(self.candidate_class).filter(self.candidate_class.split == split).count()
                if self.config['verbose']:
                    print("Candidates [Split {}]: {}".format(split, nCandidates))

    def load_gold(self, split=None):
        if not split:
            splits = self.config['splits']
        else:
            splits = [split] if not isinstance(split, list) else split
        for split in splits:
            nCandidates = self.session.query(self.candidate_class).filter(self.candidate_class.split == split).count()
            if nCandidates > 0:
                print("Split {}:".format(split))
                load_external_labels(self.session, self.candidate_class, split=split, annotator='gold')

    def featurize(self, split=None, config=None):
        if config:
            self.config = config
        if not split:
            splits = self.config['splits']
        else:
            splits = [split] if not isinstance(split, list) else split         
        featurizer = FeatureAnnotator()
        for split in splits:
            nCandidates = self.session.query(self.candidate_class).filter(self.candidate_class.split == split).count()
            if nCandidates > 0:
                F = SnorkelModel.featurize(self, featurizer, split)
                nCandidates, nFeatures = F.shape
                if self.config['verbose']:
                    print("\nFeaturized split {}: ({},{}) sparse (nnz = {})".format(split, nCandidates, nFeatures, F.nnz))
        self.featurizer = featurizer

    def generate_lfs(self, config=None):
        if config:
            self.config = config
        if self.config['source'] == 'py':
            from cdr_lfs import get_cdr_lfs
            LFs = get_cdr_lfs()
            if not self.config['include_py_only_lfs']:
                for lf in list(LFs):
                    if lf.__name__ in ['LF_closer_chem', 'LF_closer_dis', 'LF_ctd_marker_induce', 'LF_ctd_unspecified_induce']:
                        LFs.remove(lf)
                print("Removed 4 'py only' LFs...")
        elif self.config['source'] == 'nl':
            with bz2.BZ2File(os.environ['SNORKELHOME'] + '/tutorials/cdr/data/ctd.pkl.bz2', 'rb') as ctd_f:
                ctd_unspecified, ctd_therapy, ctd_marker = cPickle.load(ctd_f)
            user_lists = {
                'uncertain': ['combin', 'possible', 'unlikely'],
                'causal': ['causes', 'caused', 'induce', 'induces', 'induced', 'associated with'],
                'treat': ['treat', 'effective', 'prevent', 'resistant', 'slow', 'promise', 'therap'],
                'procedure': ['inject', 'administrat'],
                'patient': ['in a patient with', 'in patients with'],
                'weak': ['none', 'although', 'was carried out', 'was conducted', 'seems', 
                        'suggests', 'risk', 'implicated', 'the aim', 'to investigate',
                        'to assess', 'to study'],
                'ctd_unspecified': ctd_unspecified,
                'ctd_therapy': ctd_therapy,
                'ctd_marker': ctd_marker,
            }
            train_cands = self.session.query(self.candidate_class).filter(self.candidate_class.split == 0).all()
            examples = get_examples('semparse_cdr', train_cands)
            sp = SemanticParser(
                self.candidate_class, 
                user_lists, 
                beam_width=self.config['beam_width'], 
                top_k=self.config['top_k'])
            print("Generating LFs with beam_width={0}, top_k={1}".format(
                self.config['beam_width'], self.config['top_k']))
            sp.evaluate(examples,
                        show_everything=False,
                        show_explanation=False,
                        show_candidate=False,
                        show_sentence=False,
                        show_parse=False,
                        show_passing=False,
                        show_correct=False,
                        pseudo_python=False,
                        remove_paren=self.config['remove_paren'],
                        paraphrases=self.config['paraphrases'],
                        only=[])
            (correct, passing, failing, redundant, erroring, unknown) = sp.LFs
            LFs = []
            for (name, lf_group) in [('correct', correct),
                                     ('passing', passing),
                                     ('failing', failing),
                                     ('redundant', redundant),
                                     ('erroring', erroring),
                                     ('unknown', unknown)]:
                if name in self.config['include']:
                    LFs += lf_group
                    print("Keeping {0} {1} LFs...".format(len(lf_group), name))
                else:
                    if len(lf_group) > 0:
                        print("Discarding {0} {1} LFs...".format(len(lf_group), name))
            if self.config['include_py_only_lfs']:
                from cdr_lfs import LF_closer_chem, LF_closer_dis, LF_ctd_marker_induce, LF_ctd_unspecified_induce
                LFs = sorted(LFs + [LF_closer_chem, LF_closer_dis, LF_ctd_marker_induce, LF_ctd_unspecified_induce], key=lambda x: x.__name__)
                print("Added 4 'py only' LFs...")
        else:
            raise Exception("Parameter 'source' must be in {'py', 'nl'}")
        
        if self.config['max_lfs']:
            if self.config['seed']:
                np.random.seed(self.config['seed'])
            np.random.shuffle(LFs)
            LFs = LFs[:self.config['max_lfs']]
        self.LFs = LFs
        print("Using {0} LFs".format(len(self.LFs)))

    def label(self, config=None):
        if config:
            self.config = config
        
        if self.LFs is None:
            print("Running generate_lfs() first...")
            self.generate_lfs()

        while True:
            labeler = LabelAnnotator(f=self.LFs)
            for split in self.config['splits']:
                if split==TEST:
                    continue
                nCandidates = self.session.query(self.candidate_class).filter(self.candidate_class.split == split).count()
                if nCandidates > 0:
                    L = SnorkelModel.label(self, labeler, split)
                    if split==TRAIN:
                        L_train = L
                    nCandidates, nLabels = L.shape
                    if self.config['verbose']:
                        print("\nLabeled split {}: ({},{}) sparse (nnz = {})".format(split, nCandidates, nLabels, L.nnz))
                        training_set_summary_stats(L, return_vals=False, verbose=True)
            self.labeler = labeler

            lf_useless = set()
            if self.config['filter_uniform_labels']:
                for i in range(L_train.shape[1]):
                    if abs(np.sum(L_train[:,i])) == L_train.shape[0]:
                        lf_useless.add(self.LFs[i])
                
            lf_twins = set()
            if self.config['filter_redundant_signatures']:
                signatures = set()
                L_train_coo = coo_matrix(L_train)
                row = L_train_coo.row
                col = L_train_coo.col
                data = L_train_coo.data
                for i in range(L_train.shape[1]):
                    signature = hash((hash(tuple(row[col==i])),hash(tuple(data[col==i]))))
                    if signature in signatures:
                        lf_twins.add(self.LFs[i])
                    else:
                        signatures.add(signature)
                lf_twins = lf_twins.difference(lf_useless)
            
            """
            NOTE: This method of removal is a total hack. Far better would be
            to create a sane slice method for the csr_AnnotationMatrix class and
            use that to simply slice only the relevant LFs.
            """
            lf_remove = lf_useless.union(lf_twins)
            if len(lf_remove) == 0:
                break
            else:
                print("Uniform labels filter found {} LFs".format(len(lf_useless)))
                print("Redundant signature filter found {} LFs".format(len(lf_twins)))
                self.LFs = [lf for lf in self.LFs if lf not in lf_remove]
                print("Filters removed a total of {} LFs".format(len(lf_remove)))
                print("Running label step again with {} LFs...\n".format(len(self.LFs)))


    def supervise(self, config=None):
        if config:
            self.config = config

        if not self.labeler:
            self.labeler = LabelAnnotator(f=None)
        L_train = self.labeler.load_matrix(self.session, split=TRAIN)

        if self.config['traditional']:
            # Do traditional supervision with hard labels
            L_gold_train = load_gold_labels(self.session, annotator_name='gold', split=TRAIN)
            train_marginals = np.array(L_gold_train.todense()).reshape((L_gold_train.shape[0],))
            train_marginals[train_marginals==-1] = 0
        else:
            if self.config['majority_vote']:
                train_marginals = np.where(np.ravel(np.sum(L_train, axis=1)) <= 0, 0.0, 1.0)
            else:
                if self.config['model_dep']:
                    ds = DependencySelector()
                    deps = ds.select(L_train, threshold=self.config['threshold'])
                    if self.config['verbose']:
                        self.display_dependencies(deps)
                else:
                    deps = ()
            
                gen_model = GenerativeModel(lf_propensity=True)
                gen_model.train(
                    L_train, deps=deps, epochs=20, decay=0.95, 
                    step_size=0.1/L_train.shape[0], init_acc=2.0, reg_param=0.0)

                train_marginals = gen_model.marginals(L_train)
                
            if self.config['majority_vote']:
                self.LF_stats = None
            else:
                if self.config['verbose']:
                    if self.config['empirical_from_train']:
                        L = self.labeler.load_matrix(self.session, split=TRAIN)
                        L_gold = load_gold_labels(self.session, annotator_name='gold', split=TRAIN)
                    else:
                        L = self.labeler.load_matrix(self.session, split=DEV)
                        L_gold = load_gold_labels(self.session, annotator_name='gold', split=DEV)
                    self.LF_stats = L.lf_stats(self.session, L_gold, gen_model.weights.lf_accuracy())
                    if self.config['display_correlation']:
                        self.display_accuracy_correlation()
            
        save_marginals(self.session, L_train, train_marginals)

        if self.config['verbose']:
            if self.config['display_marginals']:
                # Display marginals
                plt.hist(train_marginals, bins=20)
                plt.show()

    def classify(self, config=None):
        if config:
            self.config = config

        if self.config['seed']:
            np.random.seed(self.config['seed'])

        train_marginals = load_marginals(self.session, split=TRAIN)

        if DEV in self.config['splits']:
            L_gold_dev = load_gold_labels(self.session, annotator_name='gold', split=DEV)
        if TEST in self.config['splits']:
            L_gold_test = load_gold_labels(self.session, annotator_name='gold', split=TEST)

        if self.config['model']=='logreg':
            disc_model = SparseLogisticRegression()
            self.model = disc_model

            if not self.featurizer:
                self.featurizer = FeatureAnnotator()
            if TRAIN in self.config['splits']:
                F_train =  self.featurizer.load_matrix(self.session, split=TRAIN)
            if DEV in self.config['splits']:
                F_dev =  self.featurizer.load_matrix(self.session, split=DEV)
            if TEST in self.config['splits']:
                F_test =  self.featurizer.load_matrix(self.session, split=TEST)

            if self.config['traditional']:
                train_size = self.config['traditional']
                F_train = F_train[:train_size, :]
                train_marginals = train_marginals[:train_size]
                print("Using {0} hard-labeled examples for supervision\n".format(train_marginals.shape[0]))

            if self.config['n_search'] > 1:
                lr_min, lr_max = min(self.config['lr']), max(self.config['lr'])
                l1_min, l1_max = min(self.config['l1_penalty']), max(self.config['l1_penalty'])
                l2_min, l2_max = min(self.config['l2_penalty']), max(self.config['l2_penalty'])
                lr_param = RangeParameter('lr', lr_min, lr_max, step=1, log_base=10)
                l1_param  = RangeParameter('l1_penalty', l1_min, l1_max, step=1, log_base=10)
                l2_param  = RangeParameter('l2_penalty', l2_min, l2_max, step=1, log_base=10)
            
                searcher = RandomSearch(self.session, disc_model, 
                                        F_train, train_marginals, 
                                        [lr_param, l1_param, l2_param], 
                                        n=self.config['n_search'])

                print("\nRandom Search:")
                search_stats = searcher.fit(F_dev, L_gold_dev, 
                                            n_epochs=self.config['n_epochs'], 
                                            rebalance=self.config['rebalance'],
                                            print_freq=self.config['print_freq'],
                                            seed=self.config['seed'])

                if self.config['verbose']:
                    print(search_stats)
                
                disc_model = searcher.model
                    
            else:
                lr = self.config['lr'] if len(self.config['lr'])==1 else 1e-2
                l1_penalty = self.config['l1_penalty'] if len(self.config['l1_penalty'])==1 else 1e-3
                l2_penalty = self.config['l2_penalty'] if len(self.config['l2_penalty'])==1 else 1e-5
                disc_model.train(F_train, train_marginals, 
                                 lr=lr, 
                                 l1_penalty=l1_penalty, 
                                 l2_penalty=l2_penalty,
                                 n_epochs=self.config['n_epochs'], 
                                 rebalance=self.config['rebalance'],
                                 seed=self.config['seed'])
            
            if DEV in self.config['splits']:
                print("\nDev:")
                TP, FP, TN, FN = disc_model.score(self.session, F_dev, L_gold_dev, train_marginals=train_marginals, b=self.config['b'])
            
            if TEST in self.config['splits']:
                print("\nTest:")
                TP, FP, TN, FN = disc_model.score(self.session, F_test, L_gold_test, train_marginals=train_marginals, b=self.config['b'])

        else:
            raise NotImplementedError

    def display_accuracy_correlation(self):
        empirical = self.LF_stats['Empirical Acc.'].get_values()
        learned = self.LF_stats['Learned Acc.'].get_values()
        conflict = self.LF_stats['Conflicts'].get_values()
        N = len(learned)
        colors = np.random.rand(N)
        area = np.pi * (30 * conflict)**2  # 0 to 30 point radii
        plt.scatter(empirical, learned, s=area, c=colors, alpha=0.5)
        plt.xlabel('empirical')
        plt.ylabel('learned')
        plt.show()

    def display_dependencies(self, deps_encoded):
        dep_names = {
            0: 'DEP_SIMILAR',
            1: 'DEP_FIXING',
            2: 'DEP_REINFORCING',
            3: 'DEP_EXCLUSIVE',
        }
        if not self.LFs:
            self.generate_lfs()
            print("Running generate_lfs() first...")   
        LF_names = {i:lf.__name__ for i, lf in enumerate(self.LFs)}
        deps_decoded = []
        for dep in deps_encoded:
            (lf1, lf2, d) = dep
            deps_decoded.append((LF_names[lf1], LF_names[lf2], dep_names[d]))
        for dep in sorted(deps_decoded):
            (lf1, lf2, d) = dep
            print('{:16}: ({}, {})'.format(d, lf1, lf2))

            # lfs = sorted([lf1, lf2])
            # deps_decoded.append((LF_names[lfs[0]], LF_names[lfs[1]], dep_names[d]))
        # for dep in sorted(list(set(deps_decoded))):
        #     (lf1, lf2, d) = dep
        #     print('{:16}: ({}, {})'.format(d, lf1, lf2))