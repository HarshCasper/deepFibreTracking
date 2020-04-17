import numpy as np
#import tensorflow as tf
import os
import argparse
import time
import dipy.reconst.dti as dti
import src.dwi_tools as dwi_tools
import src.tracking as tracking
import h5py
#from src.tied_layers1d import Convolution2D_tied
from src.state import TractographyInformation
#from src.nn_helper import swish, squared_cosine_proximity_2, weighted_binary_crossentropy, mse_directionInvariant, squared_cosine_proximity_WEP
#from src.SelectiveDropout import SelectiveDropout
from dipy.segment.mask import median_otsu
from dipy.core.gradients import gradient_table, gradient_table_from_bvals_bvecs
from dipy.tracking.utils import random_seeds_from_mask, seeds_from_mask
#from keras.models import load_model
#from keras.layers import Activation
from src.model import ModelLSTM, ModelMLP
import torch
import torch.nn as nn

def main():
    parser = argparse.ArgumentParser(description='Deep Learning Tractography -- Tracking')
    parser.add_argument('-c', '--caseid', default='ISMRM_2015_Tracto_challenge_data', help='name of tracking case')
    parser.add_argument('-n', '--noSteps', default=200, type=int, help='no. tracking steps')
    parser.add_argument('-b', dest='b', default=1000, type=int, help='b-value')
    parser.add_argument('-sw',dest='sw',default=1.0, type=float, help='no. tracking steps')
    parser.add_argument('-threshold',dest='threshold',default=0.5, type=float, help='pContinueTracking Threshold (between 0 and 1)')
    parser.add_argument('-mil','--minLength', type=int, default=20, help='minimal length of a streamline [mm]')
    parser.add_argument('-mal','--maxLength', type=int, default=200, help='maximum length of a streamline [mm]')
    parser.add_argument('-fa','--faThreshold', dest='fa', type=float, default=0, help='fa threshold in case of non-magical models')
    parser.add_argument('-sh', '--shOrder', dest='sh', type=int, default=4, help='order of spherical harmonics (if used)')
    parser.add_argument('-nt', '--noThreads', type=int, default=4, help='number of parallel threads of the data generator. Note: this also increases the memory demand.')
    parser.add_argument('-spc', dest='spc', default=1.0, type=float, help='grid spacing [pixels/IJK]')
    parser.add_argument('--denoise', help='denoise dataset', dest='denoise' , action='store_true')
    parser.add_argument('--reslice', help='reslice datase to 1.25mm^3', dest='reslice' , action='store_true')
    parser.add_argument('--useMLP', help='use best MLP instead of LSTM', dest='mlp' , action='store_true')
    parser.add_argument('--rotateData', help='rotate data', dest='rotateData' , action='store_true')
    parser.add_argument('--dontRotateGradients', help='rotate gradients', dest='rotateGradients', action='store_false')
    parser.add_argument('-r','--pathRecurrentNetwork', help='path to the trained recurrent network', dest='pathRecurrentNetwork', default='')
    parser.add_argument('--faMask', help='use fa mask to seed tracking',
                        dest='faMask', action='store_true')
    parser.set_defaults(denoise=False)   
    parser.set_defaults(reslice=False)   
    parser.set_defaults(rotateData=False)
    parser.set_defaults(rotateGradients=True)
    parser.set_defaults(faMask=False)
    args = parser.parse_args()

    myState = TractographyInformation()
    myState.b_value = args.b
    myState.stepWidth = args.sw
    myState.shOrder = args.sh
    myState.faThreshold = args.fa
    myState.rotateData = args.rotateData
    #myState.model = args.model
    myState.hcpID = args.caseid
    myState.gridSpacing = args.spc
    myState.resampleDWIAfterRotation = args.rotateGradients
    myState.dim = [3,3,3]
    myState.pStopTracking = args.threshold
    minimumStreamlineLength = args.minLength
    maximumStreamlineLength = args.maxLength
    noTrackingSteps = args.noSteps
    resliceDataToHCPDimension = args.reslice
    useDenoising = args.denoise

    network_string = "lstm"
    if args.mlp:
        network_string = "mlp"
    pResult = "results_tracking/" + myState.hcpID + os.path.sep + myState.model.replace('.h5','').replace('/models/','/').replace('results','') + 'reslice-' + str(resliceDataToHCPDimension) + '-denoising-' + str(useDenoising) + '-' + str(noTrackingSteps) + 'st-20mm-fa-' + str(myState.faThreshold) + "-threshold-" + str(myState.pStopTracking)+"-" + network_string
    print("Saving final data in {}".format(pResult))
    os.makedirs(pResult, exist_ok=True)

    # load DWI data
    print('Loading dataset %s at b=%d' % (myState.hcpID, myState.b_value))
    bvals,bvecs,gtab,dwi,aff,t1 = dwi_tools.loadISMRMData('data/%s' % (myState.hcpID), denoiseData = useDenoising, resliceToHCPDimensions=resliceDataToHCPDimension)
    b0_mask, binarymask = median_otsu(dwi[:,:,:,0], 2, 1)
   
    # crop DWI data
    dwi_subset, gtab_subset, bvals_subset, bvecs_subset = dwi_tools.cropDatsetToBValue(myState.b_value, bvals, bvecs, dwi)
    b0_idx = bvals < 10
    b0 = dwi[..., b0_idx].mean(axis=3)
    fa = None

    print('FA estimation')
    dwi_singleShell = np.concatenate((dwi_subset, dwi[..., b0_idx]), axis=3)
    bvals_singleShell = np.concatenate((bvals_subset, bvals[..., b0_idx]), axis=0)
    bvecs_singleShell = np.concatenate((bvecs_subset, bvecs[b0_idx,]), axis=0)
    gtab_singleShell = gradient_table(bvals=bvals_singleShell, bvecs=bvecs_singleShell, b0_threshold = 10)

    start_time = time.time()
    dti_model = dti.TensorModel(gtab_singleShell, fit_method='LS')
    dti_fit = dti_model.fit(dwi_singleShell)
    runtime = time.time() - start_time
    print('Runtime ' + str(runtime) + 's')

    fa = dti_fit.fa

    dwi_subset = dwi_tools.normalize_dwi(dwi_subset, b0)
    myState.b0 = b0
    myState.bvals = bvals_subset
    myState.bvecs = bvecs_subset

    #print('resampling to 100 directions')
    #print(str(myState.bvecs.shape))
    dwi_subset, resamplingSphere = dwi_tools.resample_dwi(dwi_subset, myState.b0, myState.bvals, myState.bvecs, sh_order=myState.shOrder, smooth=0, mean_centering=False)
    myState.bvecs = resamplingSphere.vertices

    print("Tractography")
    # whole brain seeds
    #seedsToUse = seeds_from_mask(binarymask, affine=aff)
    seedsToUse = random_seeds_from_mask(binarymask, affine=aff, seeds_count = 1000, seed_count_per_voxel = False)
    print(len(seedsToUse))
    if(args.faMask):
        print('Seeding tracking method by fa mask')
        famask = np.zeros(binarymask.shape)
        famask[fa > 0.2] = 1
        famask[binarymask == 0] = 0
        seedsToUse = seeds_from_mask(famask, affine=aff)
        pResult += '-famask'

    print("rot: " + str(myState.rotateData))
    if args.mlp:
        print("Using MLP")
        model = ModelMLP(hidden_sizes=[192,192,192,192], dropout=0.07, input_size=2700, activation_function=nn.Tanh()) #.cuda()
        model.load_state_dict(torch.load('models/model.pt', map_location='cpu'))
    else:
        print("Using LSTM")
        model = ModelLSTM(dropout=0.06, hidden_sizes=[191,191], input_size=2700, activation_function=nn.Tanh()) #.cuda()
        model.load_state_dict(torch.load('models/model.pt.lstm', map_location='cpu'))
    model.eval()
    for param in model.parameters():
        param.requires_grad = False
    start_time = time.time()
    streamlines_joined_sc, vNorms = tracking.startWithRNNBatches(myState = myState, printProgress = True, mask=binarymask, inverseDirection=False, seeds=seedsToUse, data=dwi_subset, affine=aff, model=model, noIterations = noTrackingSteps)
    runtime = time.time() - start_time
    print('Runtime ' + str(runtime) + ' s ')
   
    print("Postprocessing streamlines and removing streamlines shorter than " + str(minimumStreamlineLength) + " mm")
    streamlines_joined_sc_raw = streamlines_joined_sc
    #np.save(pResult + '_streamlines_joined_sc_raw.npy',streamlines_joined_sc_raw)
    streamlines_joined_sc = dwi_tools.filterStreamlinesByLength(streamlines_joined_sc, minimumStreamlineLength)
    streamlines_joined_sc = dwi_tools.filterStreamlinesByMaxLength(streamlines_joined_sc, maximumStreamlineLength) # 200mm
    print("The data is being written to disk.")

    #np.save(pResult + '_streamlines_joined_sc.npy',streamlines_joined_sc)
    #np.save(pResult + '_streamlines_joined_raw.npy', streamlines_joined_sc_raw)
    #np.save(pResult + '_seedsToUse.npy',seedsToUse)
    # np.save(pResult + '_probs.npy', probs)

    dwi_tools.saveVTKstreamlines(streamlines_joined_sc,pResult + '.vtk')

    #dwi_tools.saveVTKstreamlinesWithPointdata(streamlines_joined_sc_raw, pResult + '_probs.vtk', probs)


if __name__ == "__main__":
    main()
