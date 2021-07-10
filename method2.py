import sys
import copy
import random
import pandas as pd
import numpy as np
import time
import math
import itertools
import warnings

from sklearn.cluster import KMeans
from sklearn.mixture import GaussianMixture
from fisher import pvalue

import matplotlib.pyplot as plt

# optimizer
TRY_USE_NUMBA=False
# def jit_if_available(*_args, **_kwargs):
def jit_if_available(func):
    def decorated(*args, **kwargs):
        return func(*args, **kwargs)
    return decorated
    # return decorator
if TRY_USE_NUMBA:
    try:
        from numba import jit  # as jit_if_available
        jit_if_available = jit()
    except:
        print("Numba is not available. Install numba for a bit faster calculations")


def calc_SNR(ar1, ar2):
    return (np.mean(ar1) - np.mean(ar2)) / (np.std(ar1) + np.std(ar2))


def select_pos_neg(row, min_SNR, min_n_samples, min_diff_samples, stat):
    """ find 'higher' (positive), and 'lower' (negative) signal in vals. 
        vals are found with GM binarization
    """

    with warnings.catch_warnings(): # this is to ignore convergence warnings 
        warnings.simplefilter('ignore')
        
        row2d = row[:, np.newaxis]  # adding mock axis for GM interface
        labels = GaussianMixture(
            n_components=2, init_params="kmeans",
            max_iter=300, n_init = 1, 
            covariance_type = "spherical"
        ).fit(row2d).predict(row2d) # Bayesian
        
        stat['SNRs'].append(calc_SNR(row[labels==0], row[labels==1])) 
    
    # let labels == 1 be the bigger half
    if np.sum(labels == 0) > np.sum(labels == 1): 
        labels = 1 - labels
    n0 = np.sum(labels == 0)
    n1 = np.sum(labels == 1)
    assert n0 + n1 == len(row)

    signal_pretendents = []
    if min_n_samples < n0: 
        # signal (bicluster) should be big enough
        signal_pretendents.append(labels==0)
        if n1 - n0 < min_diff_samples:
            # in case of insignificant difference 
            # the bigger half is treated as signal too
            stat['n_inexplicit'] += 1 
            signal_pretendents.append(labels==1) 
            
    mask_pos = np.zeros_like(labels, bool)
    mask_neg = np.zeros_like(labels, bool)
    for mask in signal_pretendents:
        sig_snr = calc_SNR(row[mask], row[~mask])
        if abs(sig_snr) > min_SNR:
            if sig_snr > 0:
                mask_pos |= mask
            else: 
                mask_neg |= mask

    return mask_pos, mask_neg


def GM_binarization(exprs, min_SNR, min_n_samples, verbose=True, plot=True, plot_SNR_thr=2, show_fits=[]):
    t0 = time.time()
    stat = {
        'SNRs': [],
        'n_inexplicit': 0,
    }

    mask_pos, mask_neg = [], []
    for i, (gene, row) in enumerate(exprs.iterrows()):
        row = row.values
        row_mask_pos, row_mask_neg = select_pos_neg(row, min_SNR, min_n_samples, min_n_samples*2, stat)
        mask_pos.append(row_mask_pos.astype(int))
        mask_neg.append(row_mask_neg.astype(int))

        # logging
        if verbose:
            if i % 1000 == 0:
                print("\t\tgenes processed:",i)
        SNR = stat['SNRs'][-1]
        if plot and abs(SNR) > plot_SNR_thr or gene in show_fits:
            print("Gene %s: SNR=%s, pos=%s, neg=%s"%(gene, round(SNR,2), len(row_mask_pos), len(row_mask_neg)))
            row_mask_neutral = (~row_mask_pos) & (~row_mask_neg)

            plt.hist(row[row_mask_neutral], bins=80, alpha=0.5, color='grey')
            plt.hist(row[row_mask_neg], bins=80, alpha=0.5, color='blue')
            plt.hist(row[row_mask_pos], bins=80, alpha=0.5, color='red')
            plt.show()
 
    def _remove_empty_rows(df):
        # thx https://stackoverflow.com/a/22650162/7647325
        return df.loc[~(df==0).all(axis=1)]
    df_p = _remove_empty_rows(pd.DataFrame(mask_pos, index=exprs.index)).T
    df_n = _remove_empty_rows(pd.DataFrame(mask_neg, index=exprs.index)).T

    # logging
    if verbose:
        print("Total runtime",round(time.time()-t0,2), "s for ", len(exprs),"genes")
        print("Genes passed SNR threshold of %s:"%round(min_SNR,2))
        print("\tup-regulated genes:", df_p.shape[1])
        print("\tdown-regulated genes:", df_n.shape[1])
        print("\tinexplicit genes:", stat['n_inexplicit'])
    if plot:
        plt.figure(figsize=(7,5))
        # plt.hist(SNRs_up, bins=50, range=(0,3), color="red", alpha=0.5) 
        # plt.hist(SNRs_down, bins=50, range=(0,3), color="blue", alpha=0.5)
        plt.hist(stat['SNRs'], bins=50, range=(0,3), color="grey", alpha=0.5)
        plt.xlabel("avg.|SNR|")
        plt.xlabel("binarized genes")
        plt.plot()

    return {"UP":df_p, "DOWN":df_n}


################## 2. Probabiliatic clustering #############
@jit_if_available
def calc_lp(gene_ndx,module_ndx,gene2Samples,
            nOnesPerSampleInModules,moduleSizes,
            moduleOneFreqs,p0,match_score,mismatch_score,bK_1,N,
            alpha,beta_K):
    
    # 1. Prepare vals: (n_ones_per_pat, m_size, alpha, gene_vector, beta_K)
    #      and return answer in special cases
    
    m_size = moduleSizes[module_ndx]
    if m_size == 0:
        # gene is removed, the module is empty
        return p0
    
    gene_vector = gene2Samples[gene_ndx,] 
    n_ones_per_pat = nOnesPerSampleInModules[module_ndx,]
    if gene_ndx == module_ndx: # remove the gene from its own module
        if m_size == 1:
            return p0
        m_size -=1
        n_ones_per_pat = n_ones_per_pat - gene_vector

    # if a module is composed of a single gene
    if m_size == 1:
        # just count number of matches and mismatches and
        # n_matches =  np.inner(n_ones_per_pat,gene_vector)
        n_matches =  np.sum(n_ones_per_pat[gene_vector==1])

        return n_matches*match_score + (N-n_matches)*mismatch_score + bK_1
    
    # 2. usual case if a module contains more than one gene
    return calc_lp_formula(n_ones_per_pat, m_size, alpha, gene_vector, beta_K)

@jit_if_available
def calc_lp_formula(n_ones_per_pat, m_size, alpha, gene_vector, beta_K): 
    beta_term = math.log(m_size+beta_K)
    
    # alpha_term
    # ones-matching
    oneRatios = (n_ones_per_pat+alpha/2)/(m_size+alpha)
    ones_matching_term = np.sum(np.log(oneRatios)[gene_vector == 1])

    # zero-matching
    # zeroRatios = (m_size-n_ones_per_pat+alpha/2)/(m_size+alpha)
    zeroRatios = 1 - oneRatios
    zeros_matching_term = np.sum(np.log(zeroRatios)[gene_vector == 0])

    return ones_matching_term + zeros_matching_term + beta_term

def calc_lp_column(module_ndx,gene2Samples,
            nOnesPerSampleInModules,moduleSizes,
            moduleOneFreqs,p0,match_score,mismatch_score,bK_1,N,
            alpha,beta_K):
    """Same as calc_lp, but for all the genes simultaneously"""
    m_size = moduleSizes[module_ndx]
    if m_size == 0:
        # all genes are removed, the module is empty, alfl answers are p0
        return p0

    n_ones_per_pat = nOnesPerSampleInModules[module_ndx,]
    if m_size == 1:
        # if a module is composed of a single gene
        n_matches = np.dot(gene2Samples, n_ones_per_pat)
        vals = n_matches*match_score + (N-n_matches)*mismatch_score + bK_1

    else: 
        # ones-matching
        oneRatios = (n_ones_per_pat+alpha/2)/(m_size+alpha)
        # ones_matching_term = np.dot((gene2Samples == 1), np.log(oneRatios))
        ones_matching_term = np_faster_dot(gene2Samples == 1, np.log(oneRatios))

        # zero-matching
        zeroRatios = 1 - oneRatios  # = (m_size-n_ones_per_pat+alpha/2)/(m_size+alpha)
        # zeros_matching_term = np.dot((gene2Samples == 0), np.log(zeroRatios))
        zeros_matching_term = np_faster_dot(gene2Samples == 0, np.log(zeroRatios))

        beta_term = math.log(m_size+beta_K)
        vals = ones_matching_term + zeros_matching_term + beta_term

    # calc LP[m,m] with the less optimized func
    vals[module_ndx] = calc_lp(module_ndx,module_ndx,gene2Samples,nOnesPerSampleInModules,moduleSizes,
                            moduleOneFreqs,p0,match_score,mismatch_score,bK_1,N,alpha,beta_K)
    return vals

@jit_if_available
def np_faster_dot(np_a, np_b): 
    # translating to float64 for jit compilation
    # may be a bit faster in some cases
    return np.dot(
        np_a.astype(np.float64),
        np_b.astype(np.float64)
    )

def set_initial_conditions(df, alpha,beta_K,verbose = True):
    t_0 = time.time()
    N = df.shape[0] # number of samples
    K = df.shape[1] # initial number of modules
    p0 = N*np.log(0.5)+np.log(beta_K)
    match_score = np.log((alpha*0.5+1)/(alpha))
    mismatch_score = np.log((alpha*0.5+0)/alpha)
    bK_1 = math.log(1+beta_K)
    print("\t\tKxN=%sx%s"%(K,N))
    print("\t\tp0=",p0)
    
    # p0, match_score, mismatch_score, bK_1
    genes = df.columns.values
    # 1. the number of genes inside each component, initially 1 for each gene
    moduleSizes=np.ones(K,dtype=np.int)
    
    # 2. a binary (int) matrix of size KxN that indicates the samples genes
    gene2Samples = df.T.values
    
    # 3. a binary matrix of size K by m that stores the total number of ones per sample in each module,
    # initially equal to 'gene2Samples'
    nOnesPerSampleInModules = copy.copy(gene2Samples)

    #4. initial module id
    gene2Module = list(range(0,K))
    gene2Module = np.array(gene2Module)
    
    #5. moduleOneFreqs
    moduleOneFreqs = []
    for g in range(0,K):
        moduleOneFreqs.append(float(sum(gene2Samples[g,]))/N)
    moduleOneFreqs = np.array(moduleOneFreqs)
    
    #6. setting initial LPs: i = gene, j=module
    LP = np.zeros((K,K),dtype=np.float)
    for i in range(0,K):
        if (i+1)% 1000==0:
                print("\t",i+1,"genes processed in ",round(time.time()- t_0,1),"s")
        for j in range(i,K):
            LP[i,j] = calc_lp(i,j,gene2Samples,
            nOnesPerSampleInModules,moduleSizes,
            moduleOneFreqs,p0,match_score,mismatch_score,bK_1,N,alpha, beta_K)
            LP[j,i] = LP[i,j]
    print("time:\tInitial state created in",round(time.time()-t_0, 1) , "s.", file = sys.stdout)
    return moduleSizes, gene2Samples, nOnesPerSampleInModules, gene2Module, moduleOneFreqs, LP

def adjust_lp(log_probs,n_exp_orders=7):
    # adjusting the log values before normalization to avoid under-flow
    max_p = max(log_probs)
    probs = []
    for lp in log_probs:
        # shift all probs to set max_prob less than log(max_np.float)  
        adj_lp = lp - max_p
        # set to minimal values all which less then 'n_orders' lesser than p_max to zeroes
        if adj_lp >= - n_exp_orders:
            probs.append(np.exp(adj_lp))
        else:
            probs.append(0)
    probs = probs/sum(probs)
    return probs

### functions for checking of convergence conditions ###
def calc_p_transitions(states,unique,counts):
    n_steps = len(states)-1
    transitions = dict(zip(tuple(itertools.product(unique,unique)),np.zeros(len(unique)**2)))
    for i in range(0,n_steps):
        transitions[(states[i],states[i+1])] += 1 
    p = { k:v/(counts[unique.index(k[0])]) for k, v in transitions.items()}
    return  p

def collect_all_p(labels):
    P={}
    # calculate edge transition probabilities
    for edge in range(0,labels.shape[1]):
        states = labels[:,edge]
        unique,counts = np.unique(states , return_counts=True)
        if len(unique)> 1:
            P[edge] = calc_p_transitions(states,list(unique),counts)
    return P

def calc_RMSD(P,P_prev):
    t0 = time.time()
    p_prev_edges = set(P_prev.keys())
    p_edges = set(P.keys())
    Pdiff = []
    for edge in p_edges.difference(p_prev_edges):
        P_prev[edge] = {k:0 for k in P[edge].keys()}
        P_prev[edge] = {k:1 for k in P_prev[edge].keys() if k[0]==k[1]}
    for edge in p_prev_edges.difference(p_edges):
        P[edge] = {k:0 for k in P_prev[edge].keys()}
        P[edge] = {k:1 for k in P[edge].keys() if k[0]==k[1]}
    for edge in p_edges.intersection(p_prev_edges):
        p_modules = set(P[edge].keys())
        p_prev_modules = set(P_prev[edge].keys())
        for  m,m2 in p_modules.difference(p_prev_modules):
            Pdiff.append((P[edge][(m,m2)])**2) 
        for  m,m2 in p_prev_modules.difference(p_modules):
            Pdiff.append((P_prev[edge][(m,m2)])**2) 
        for  m,m2 in p_modules.intersection(p_prev_modules):
            Pdiff.append((P[edge][(m,m2)] - P_prev[edge][(m,m2)])**2) 
    if not len(Pdiff)==0:
        return np.sqrt(sum(Pdiff)/len(Pdiff))
    else:
        return 0

def check_convergence_conditions(n_skipping_edges,n_skipping_edges_range,
                                P_diffs,P_diffs_range,step,tol=0.05, verbose = True):
    n_points = len(n_skipping_edges)
    # check skipping edges 
    se_min, se_max = n_skipping_edges_range
    n_skipping_edges = np.array(n_skipping_edges,dtype=float)

    
    # scale
    n_skipping_edges = (n_skipping_edges-se_min)/(se_max - se_min)*n_points
    
    # fit line
    A = np.vstack([range(0,n_points), np.ones(n_points)]).T
    k,b = np.linalg.lstsq(A, n_skipping_edges, rcond=None)[0]
    
    # check P_diffs
    #P_diffs_min, P_diffs_max = P_diffs_range
    #P_diffs = np.array(P_diffs)
    
    # scale 
    #P_diffs = (P_diffs-P_diffs_min)/(P_diffs_max- P_diffs_min)*n_points
    #k2, b2  = np.linalg.lstsq(A, P_diffs, rcond=None)[0]
    
    if abs(k)<tol:# and abs(k2)<tol:
        convergence = True
    else:
        convergence = False
    if verbose:
        print("\tConverged:",convergence,"#skipping edges slope:",round(k,5))#,"RMS(Pn-Pn+1) slope:",round(k2,5))
    return convergence  

# @jit_if_available
def apply_changes(gene_ndx,new_module,curr_module,LP,gene2Samples, gene2Module, nOnesPerPatientInModules,moduleSizes,
                moduleOneFreqs, p0,match_score,mismatch_score, bK_1,alpha,beta_K,N,K, calc_LPs=True):
    """Moves the gene from the current module to the new one
        and updates gene2Module, nOnesPerPatientInModules and moduleSizes respectively.
        K - quantity of genes
        M (=K) - quantitiy of modules
        S - quantity of samples (patients)
        
        see also set_initial_conditions for more info

    Args: 
        gene_ndx(int): 
        new_module(int): 
        curr_module(int):
        LP(np.array KxM): matrix of transmission probabilities
        gene2Samples(np.array KxS): matrix of samples co-regulated with genes
        gene2Module(list): list of i'th module
        nOnesPerPatientInModules(np.array MxS): precalculated sums for optimization
            nOnesPerPatientInModules[m] = sum(gene2Samples[g] for g in genes if gene2Module[g] == m)
        moduleSizes(np.array M) - genes in module
        
        moduleOneFreqs - is not used
        N - is not used

        p0(float) - precalculated default probability
        match_score(float) - precalculated const
        mismatch_score(float) - precalculated const
        bK_1(float) - precalculated const
        
        alpha(float) - alg. global parameter
        beta_K(float) - alg. global parameter

        K(int) - count of Genes

        calc_LPs(bool) - should LPs be recalculated? 

    """
    # update the gene module membership
    gene2Module[gene_ndx] = new_module
    # for this edge no probabilities change
    
    # reduce curr_module size and nOnesPerPatientInModules
    gene_vector = gene2Samples[gene_ndx,]
    nOnesPerPatientInModules[curr_module,] = nOnesPerPatientInModules[curr_module,] - gene_vector
    moduleSizes[curr_module,]-=1
    
    # increase new_module
    nOnesPerPatientInModules[new_module,] = nOnesPerPatientInModules[new_module,] + gene_vector
    moduleSizes[new_module,]+=1
    
    # update LPs for all genes contacting curr and new modules
    if calc_LPs:
        for module in curr_module, new_module:
            ndx_old_val = LP[gene_ndx, module] # for this gene no probabilities changed
            LP[:, module] = calc_lp_column(module,gene2Samples,nOnesPerPatientInModules,moduleSizes,
                                                            moduleOneFreqs,p0,match_score,mismatch_score,bK_1,N,alpha,beta_K)
            LP[gene_ndx, module] = ndx_old_val






def sampling(LP,gene2Module, gene2Samples,nOnesPerPatientInModules,moduleSizes,
             moduleOneFreqs, p0, match_score, mismatch_score, bK_1, alpha, beta_K,
             max_n_steps=100,n_steps_averaged = 20,n_points_fit = 10,tol = 0.05,
             n_steps_for_convergence = 5,verbose=True):
    
    # gene2Module = np.array(gene2Module)
    K = len(gene2Module)
    N  = gene2Samples.shape[1]
    t_ =  time.time()
    gene2Module_history = [copy.copy(gene2Module)]
    is_converged = False
    #network_edges = network.edges(data=True)
    for step in range(1, max_n_steps):
        if verbose:
            print("step", step,file = sys.stdout)
        not_changed_genes = 0
        t_0 = time.time()
        t_1=t_0
        i = 1
        for gene_ndx in range(0, K):
            # adjust LogP and sample a new module
            P_adj = adjust_lp(LP[gene_ndx,:], n_exp_orders=7)
            curr_module = gene2Module[gene_ndx]
            new_module = np.random.choice(range(0,K), p=P_adj) 

            # update network and matrices if necessary
            if new_module != curr_module:
                apply_changes(gene_ndx,new_module,curr_module,LP,gene2Samples, gene2Module, nOnesPerPatientInModules,moduleSizes,
                moduleOneFreqs, p0,match_score,mismatch_score, bK_1,alpha,beta_K, N, K)
                
            else:
                not_changed_genes +=1#
            i+=1
            if i%1000 == 0:
                if verbose:
                    print(i,"\t\tgenes processed in",round(time.time()- t_1, 1) , "s runtime...",file=sys.stdout)
                not_changed_edges=0
                t_1 = time.time()
        if verbose:
            print("\tstep ",step,# 1.0*not_changed_edges/len(edge_order),"- % edges not changed; runtime",
                  round(time.time() - t_0, 1) , "s", file = sys.stdout)
        
        gene2Module_history.append(copy.copy(gene2Module))
        if step == n_steps_averaged:
            is_converged = False
            n_times_cc_fulfilled = 0
            labels = np.asarray(gene2Module_history[step-n_steps_averaged:step])
            P_prev = collect_all_p(labels)
            P_diffs = []
            n_skipping_edges = [] 
            n_skipping_edges.append(len(P_prev.keys()))
        if step > n_steps_averaged:
            labels = np.asarray(gene2Module_history[step-n_steps_averaged:step])
            P = collect_all_p(labels)
            P_diff = calc_RMSD(copy.copy(P),copy.copy(P_prev))
            P_diffs.append(P_diff)
            n_skipping_edges.append(len(P.keys()))
            P_prev=P
        if  step >= n_steps_averaged + n_points_fit:
            P_diffs_range = min(P_diffs),max(P_diffs)
            n_skipping_edges_range= min(n_skipping_edges), max(n_skipping_edges)
            # check convergence condition
            is_converged = check_convergence_conditions(n_skipping_edges[-n_points_fit:],
                                                      n_skipping_edges_range,
                                                      P_diffs[-n_points_fit:],
                                                      P_diffs_range,
                                                      step,
                                                      tol=tol,
                                                      verbose = verbose)
        if is_converged:
            n_times_cc_fulfilled +=1
        else:
            n_times_cc_fulfilled = 0
            
        if n_times_cc_fulfilled == n_steps_for_convergence: # stop if convergence is True for the last n steps
            ### define how many the last steps to consider
            n_final_steps = n_points_fit+n_steps_for_convergence
            if verbose:
                print("The model converged after", step,"steps.", file = sys.stdout)
                print("Consensus of last",n_final_steps,"states will be taken")
                print("Sampling runtime",round(time.time()- t_ ,1) , "s", file = sys.stdout)
            return gene2Module_history, n_final_steps,n_skipping_edges,P_diffs
    
    n_final_steps = n_steps_for_convergence
    if verbose:
        print("The model did not converge after", step,"steps.", file = sys.stdout)
        print("Consensus of last",n_final_steps,"states will be taken")
        print("Sampling runtime",round(time.time()- t_ ,1) , "s", file = sys.stdout)
        
    return gene2Module_history,n_final_steps,n_skipping_edges,P_diffs


def plot_convergence(n_skipping_edges,P_diffs, thr_step,n_steps_averaged, outfile = ""):
    # plots numnber of oscilating edges and RMS(Pn-Pn+1)
    steps = range(n_steps_averaged,n_steps_averaged+len(n_skipping_edges))
    fig, axarr = plt.subplots(2, 1,sharex=True, figsize=(15,7))
    axarr[0].set_title("Model convergence")
    axarr[0].plot(steps, n_skipping_edges,'b.-')
    axarr[0].axvline(thr_step,color="red",linestyle='--') 
    axarr[0].set_ylabel("#genes oscilating on the last "+str(int(n_steps_averaged))+" steps")
    steps = range(n_steps_averaged,n_steps_averaged+len(P_diffs))
    axarr[1].plot(steps,P_diffs,'b.-' )
    axarr[1].set_xlabel('step')
    axarr[1].axvline(thr_step,color="red",linestyle='--') 
    tmp = axarr[1].set_ylabel("RMS(Pn-Pn+1)")
    if outfile:
        plt.savefig(outfile, transparent=True)                        

def get_consensus_modules(gene2module_history, LP,  genes2Samples, gene2Module,
                          nOnesPerPatientInModules,moduleSizes, moduleOneFreqs, p0, match_score,mismatch_score,
                          bK_1,alpha,beta_K, N, K):
    # gene2Module = np.array(gene2Module)
    consensus = []
    labels = np.asarray(gene2module_history)
    
    # identify modules which genes ocsilate
    K = len(gene2Module)
    #genes = range(0,K)
    for gene_ndx in range(0,K):
        unique, counts = np.unique(labels[:,gene_ndx], return_counts=True)
        if len(unique) >1:
            counts = np.array(counts)
            new_ndx = unique[np.argmax(counts)]
            if float(max(counts))/labels.shape[0] < 0.5: 
                print("Warning: less than 50% of time in the most frequent module\n\tedge:",gene_ndx,
                      "counts:",counts,"\n\tlabels:" , ",".join(map(str,unique)) ,file= sys.stdout)
            consensus.append(new_ndx)
        else:
            consensus.append(unique[0])
            
    # construct consensus edge-to-module membership
    changed_genes = 0
    for m in range(0,len(consensus)):
        curr_module = gene2Module[m]
        new_module = consensus[m]
        if curr_module != new_module:
            changed_genes += 1
            # move genes to their consensus modules

            apply_changes(gene_ndx,new_module,curr_module,LP,genes2Samples, gene2Module, nOnesPerPatientInModules,moduleSizes,
                moduleOneFreqs, p0,match_score,mismatch_score, bK_1,alpha,beta_K, N, K, calc_LPs=False)
            
    print(changed_genes, "genes changed their module membership after taking consensus.")
    return consensus, nOnesPerPatientInModules, moduleSizes, moduleOneFreqs
        
################################## 3. Post-processing ####################

def genesets2biclusters(exprs_np, exprs_data,moduleSizes,consensus_gene2module,
                        min_SNR = 0.5,direction="UP",min_n_samples=10,
                        verbose = True):
    # Identify optimal sample set for each module: split samples into two sets in a subspace of each module
    # Filter out bad biclusters with too few genes or samples, or with low SNR
    t0 = time.time()
    filtered_bics = []
    few_genes = 0
    empty_bics = 0
    wrong_sample_number = 0
    low_SNR = 0
    
    for mid in range(0,len(moduleSizes)):
        if moduleSizes[mid]>1: # exclude biclusters with too few genes
            bic_gene_ids = [i for i, j in enumerate(consensus_gene2module) if j == mid] 
            bic = identify_opt_sample_set(bic_gene_ids, exprs_np, exprs_data,
                                          direction=direction,
                                          min_n_samples=min_n_samples)
            avgSNR = bic["avgSNR"]
            if avgSNR ==-1:  # exclude biclusters with too few samples
                wrong_sample_number+=1
            elif avgSNR < min_SNR: # exclude biclusters with low avg. SNR 
                low_SNR += 1
            else:
                bic["id"] = mid
                filtered_bics.append(bic)
        elif moduleSizes[mid]>0:
            few_genes += 1 
        else: 
            empty_bics +=1
      
    if verbose:
        print("time:\tIdentified optimal sample sets for %s modules in %s s." %(len(moduleSizes),round(time.time()-t0,2)))
        
        print("\tEmpty modules:",few_genes, file = sys.stdout)
        print("\tModules with just 1 edge:",few_genes, file = sys.stdout)
        print("\tModules with not enough or too many samples:",wrong_sample_number, file = sys.stdout)      
        print("\tModules not passed avg. |SNR| threshold:", low_SNR, file = sys.stdout)

        print("Passed modules with >= 2 genes and >= %s samples: %s"%(min_n_samples,len(filtered_bics)), file = sys.stdout)
    return filtered_bics


def identify_opt_sample_set(bic_genes,exprs,exprs_data,direction="UP",min_n_samples=8):
    # identify optimal samples set given gene set
    N, exprs_sums, exprs_sq_sums = exprs_data
    e = exprs[bic_genes,:]
    
    labels = KMeans(n_clusters=2, random_state=0).fit(e.T).labels_
    ndx0 = np.where(labels == 0)[0]
    ndx1 = np.where(labels == 1)[0]
    if min(len(ndx1),len(ndx0))< min_n_samples:
        return {"avgSNR":-1}
    if np.mean(e[:,ndx1].mean()) > np.mean(e[:,ndx0].mean()):
        if direction=="UP":samples = ndx1
        else: samples = ndx0
    else:
        if direction=="UP":samples = ndx0
        else: samples = ndx1
    avgSNR = calc_bic_SNR(bic_genes, samples, exprs, N, exprs_sums, exprs_sq_sums)

    if len(samples)>=min_n_samples: # len(samples)<N*0.5*1.1 - allow bicluster to be a little bit bigger than N/2
        bic = {"genes":set(bic_genes),"n_genes":len(bic_genes),
               "samples":set(samples),"n_samples":len(samples),
               "avgSNR":avgSNR,"direction":direction}
        return bic
    else:
        return {"avgSNR":-1}
    
def calc_bic_SNR(genes, samples, exprs, N, exprs_sums,exprs_sq_sums):
    bic = exprs[genes,:][:,samples]
    bic_sums = bic.sum(axis=1)
    bic_sq_sums = np.square(bic).sum(axis=1)

    bg_counts = N - len(samples)
    bg_sums = exprs_sums[genes]-bic_sums
    bg_sq_sums = exprs_sq_sums[genes]-bic_sq_sums
    
    bic_mean, bic_std = calc_mean_std_by_powers((len(samples),bic_sums,bic_sq_sums))
    bg_mean, bg_std = calc_mean_std_by_powers((bg_counts,bg_sums,bg_sq_sums))
    
    return  np.mean(abs(bic_mean - bg_mean)/ (bic_std + bg_std))

def calc_mean_std_by_powers(powers):
    count, val_sum, sum_sq = powers

    mean = val_sum / count  # what if count == 0?
    std = np.sqrt((sum_sq / count) - mean*mean)
    return mean, std

###### save and read modules #####
def write_bic_table(resulting_bics, results_file_name):
    if len(resulting_bics) ==0 :
        pass
    else:
        resulting_bics = pd.DataFrame.from_dict(resulting_bics)
        resulting_bics["genes"] = resulting_bics["genes"].apply(lambda x:" ".join(map(str,x)))
        resulting_bics["samples"] = resulting_bics["samples"].apply(lambda x:" ".join(map(str,x)))
        resulting_bics = resulting_bics[["id","avgSNR","n_genes","n_samples","direction","genes","samples"]]
        resulting_bics.sort_values(by=["avgSNR","n_genes","n_samples"],inplace = True, ascending = False)
        resulting_bics["id"] = range(0,resulting_bics.shape[0])
    resulting_bics.to_csv(results_file_name ,sep = "\t", index=False)

def read_bic_table(results_file_name):
    if not os.path.exists(results_file_name):
        return pd.DataFrame()
    resulting_bics = pd.read_csv(results_file_name,sep = "\t")
    if len(resulting_bics) ==0:
        return pd.DataFrame()
    else:
        resulting_bics["genes"] = resulting_bics["genes"].apply(lambda x: set(x.split(" ")))
        resulting_bics["samples"] = resulting_bics["samples"].apply(lambda x: set(x.split(" ")))
    #resulting_bics.set_index("id",inplace=True)
    
    return resulting_bics
