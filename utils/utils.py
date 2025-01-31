import os
import sys
import json
import pickle
import random
from utils.functions import calculate_loss

import torch
from tqdm import tqdm
import torch.nn as nn
import matplotlib.pyplot as plt

import numpy as np
import random
import sys
import torchvision.models as models
from losses.losses import FocalLoss
from args import get_args
from utils.evaluate import compute_confusion_matrix,compute_indexes
from sklearn.metrics import roc_auc_score
from models.gate_funs.noisy_gate import NoisyGate
from models.gate_funs.noisy_gate_vmoe import NoisyGate_VMoE
from torchmetrics import MetricCollection,Recall,Specificity,AUROC

# from seresnext import se_resnext50_32x4d
args=get_args()
save_pth = args.res_savedir


if args.backbone == 'vit':
    resnet = models.__dict__[args.arch](pretrained=True).to('cuda')
    resnet = torch.nn.Sequential(*(list(resnet.children())[:-1])).to('cuda')
    if os.path.isfile(args.res_weights):
        resnet.load_state_dict(torch.load(args.res_weights))


# resnet = torch.nn.Sequential(*(list(resnet.children())[:-1])).to('cuda')
def pre_cls_model(x):
    features = resnet(x)
    #print('sad',features.size())
    return features.squeeze()


softmax = nn.Softmax(dim=1)

device_gpu = torch.device('cuda')
device_cpu = torch.device('cpu')


def get_lossfn(name,use_reduction=False):
    if name == 'CELoss':
        return nn.CrossEntropyLoss(reduction = args.reduction if use_reduction else 'mean')
    elif name == 'FocalLoss':
        return FocalLoss(gamma=2,alpha=0.25,size_average=False)

# Choosing `num_centers` random data points as the initial centers
def random_init(dataset, num_centers):
    num_points = dataset.size(0)
    dimension = dataset.size(1)
    used = torch.zeros(num_points, dtype=torch.long)
    indices = torch.zeros(num_centers, dtype=torch.long)
    for i in range(num_centers):
        while True:
            cur_id = random.randint(0, num_points - 1)
            if used[cur_id] > 0:
                continue
            used[cur_id] = 1
            indices[i] = cur_id
            break
    indices = indices.to(device_gpu)
    centers = torch.gather(dataset, 0, indices.view(-1, 1).expand(-1, dimension))
    return centers


# Compute for each data point the closest center
def compute_codes(dataset, centers):
    num_points = dataset.size(0)
    dimension = dataset.size(1)
    num_centers = centers.size(0)
    # 5e8 should vary depending on the free memory on the GPU
    # Ideally, automatically ;)
    chunk_size = int(5e8 / num_centers)
    codes = torch.zeros(num_points, dtype=torch.long, device=device_gpu)
    centers_t = torch.transpose(centers, 0, 1)
    centers_norms = torch.sum(centers ** 2, dim=1).view(1, -1)
    for i in range(0, num_points, chunk_size):
        begin = i
        end = min(begin + chunk_size, num_points)
        dataset_piece = dataset[begin:end, :]
        dataset_norms = torch.sum(dataset_piece ** 2, dim=1).view(-1, 1)
        distances = torch.mm(dataset_piece, centers_t)
        distances *= -2.0
        distances += dataset_norms
        distances += centers_norms
        _, min_ind = torch.min(distances, dim=1)
        codes[begin:end] = min_ind
    return codes


# Compute new centers as means of the data points forming the clusters
def update_centers(dataset, codes, num_centers):
    num_points = dataset.size(0)
    dimension = dataset.size(1)
    centers = torch.zeros(num_centers, dimension, dtype=torch.float, device=device_gpu)
    cnt = torch.zeros(num_centers, dtype=torch.float, device=device_gpu)
    centers.scatter_add_(0, codes.view(-1, 1).expand(-1, dimension), dataset)
    cnt.scatter_add_(0, codes, torch.ones(num_points, dtype=torch.float, device=device_gpu))
    # Avoiding division by zero
    # Not necessary if there are no duplicates among the data points
    cnt = torch.where(cnt > 0.5, cnt, torch.ones(num_centers, dtype=torch.float, device=device_gpu))
    centers /= cnt.view(-1, 1)
    return centers


def cluster(dataset, num_centers):
    centers = random_init(dataset, num_centers)
    codes = compute_codes(dataset, centers)
    num_iterations = 0
    while True:
        num_iterations += 1
        centers = update_centers(dataset, codes, num_centers)
        new_codes = compute_codes(dataset, centers)
        # Waiting until the clustering stops updating altogether
        # This is too strict in practice
        if torch.equal(codes, new_codes):
            break
        codes = new_codes
    return codes


def train_one_epoch_multi_longtail_weight(model, optimizer, data_loader, device, epoch, multi_tasks,istrain=[True]*args.multi_tasks,cont=False):
    model.train()
    
    # loss_function = torch.nn.BCELoss()#torch.nn.CrossEntropyLoss()
    loss_functions = args.loss_fns
    accu_loss = torch.zeros(1).to(device)  # 累计损失
    accu_num = torch.zeros(1).to(device)  # 累计预测正确的样本数
    optimizer.zero_grad()

    sample_num = 0
    data_loader = tqdm(data_loader, file=sys.stdout)
    accu_num = [0] * multi_tasks
    accu_loss = [0] * multi_tasks
    errors_nums = [0] * multi_tasks
    errors_lists = [ [] for i in range(multi_tasks) ]
    for step, data in enumerate(data_loader):
        images, labels = data
        sample_num += images.shape[0]
        x = images.to(device)
        B = x.shape[0]

        if args.backbone in ('vit','TransMIL'):
            resnet.eval()
            x = torch.reshape(x, (x.shape[0] * x.shape[1], x.shape[2], x.shape[3], x.shape[4]))
            features = pre_cls_model(x)
            features = torch.reshape(features, (B, int(features.shape[0] / B), features.shape[1]))
        elif args.backbone in ('vit_res','vit_moe'):
            features = x


        preds = model(features)

        loss_total = 0
    
        labels = list(labels.values())

        for i in range(multi_tasks):
            # print(i,args.loss_fns[i],labels[-1])
            loss_function = get_lossfn(loss_functions[i],use_reduction=True)
            pred = preds[i].to(device)
            label = labels[i].to(device).long()
            # pred = torch.clamp(pred, min=1e-4, max=1 - 1e-4)

            pred_classes = torch.max(torch.softmax(pred, dim=1), dim=1)[1]
            accu_num[i] += torch.eq(pred_classes, label.to(device)).sum().item()
            if istrain[i]:
                loss,errors_num,error_name = calculate_loss(loss_function,pred,label,args.tasks[i],args.loss_weights[i],args.reduction,labels,cont)
                loss_total += loss
                accu_loss[i] += loss.detach().item()
                errors_nums[i] += errors_num
                errors_lists[i].extend(error_name)

        loss_total.backward()
        

        # s = ''.join([' loss_{}: {:.3f}, acc_{}: {:.3f}'.format(i,acc_l/ (step + 1),i,acc_n/ sample_num) for i,(acc_l,acc_n) in enumerate(zip(accu_loss,accu_num))])
        s = ''.join([' loss_{}: {:.3f}, acc_{}: {:.3f}'.format(i,accu_loss[i]/ (step + 1),i,accu_num[i]/ sample_num) for i in args.show_tasks])
 
        s_desc = f'[train epoch {epoch}] '+ s 
        if cont:
            s_desc += f' errors: {errors_nums}'
        data_loader.desc = s_desc

        if not torch.isfinite(loss_total):
            print('WARNING: non-finite loss, ending training ', loss_total)
            sys.exit(1)

        optimizer.step()
        optimizer.zero_grad()
    
    if cont:
        err_res = ''.join([f'total_errors{i} : {errors_nums[i]} {len(errors_lists[i])} ' for i in args.show_tasks])
        print(err_res)
        #print(f' total errors :{errors_nums[1]} ,{len(errors_lists[1])}')
    return np.array(accu_loss) / (step + 1), np.array(accu_num) / sample_num, errors_lists

    


@torch.no_grad()
def evaluate_multi_longtail_weight(model, data_loader, device, epoch, multi_tasks,name='valid',cont=False):
    loss_function = torch.nn.CrossEntropyLoss()

    model.eval()
    accu_num = torch.zeros(1).to(device)  # 累计预测正确的样本数
    accu_loss = torch.zeros(1).to(device)  # 累计损失
    zhenyin = 0
    gjb = 0
    sample_num = 0
    louzhen = 0
    data_loader = tqdm(data_loader, file=sys.stdout)
    accu_loss = [0] * multi_tasks
    accu_num = [0] * multi_tasks
    loss_functions = args.loss_fns
    errors_nums = [0] * multi_tasks
    errors_lists = [ [] for i in range(multi_tasks) ]

    sens_mertics = [ Recall(task='multiclass' if i>2 else 'binary',num_classes=i,average='micro').to(device) for i in args.num_classes ]
    spec_mertics = [ Specificity(task='multiclass' if i>2 else 'binary',num_classes=i,average='micro').to(device) for i in args.num_classes ]

    for step, data in enumerate(data_loader):
        images, labels = data
        sample_num += images.shape[0]
        
        x = images.to(device)
        
        B = x.shape[0]
        
        if args.backbone in ('vit','TransMIL'):
            resnet.eval()
            x = torch.reshape(x, (x.shape[0] * x.shape[1], x.shape[2], x.shape[3], x.shape[4]))
            features = pre_cls_model(x)
            features = torch.reshape(features, (B, int(features.shape[0] / B), features.shape[1]))
        elif args.backbone in ('vit_res','vit_moe'):
            features = x
 
        labels = list(labels.values())
        preds = model(features)

        for i in range(multi_tasks):
            
            # print(i,'dwd')
            loss_function = get_lossfn(loss_functions[i])
            pred = preds[i]
            label = labels[i]
            label = label.long().to(device)
            if torch.all(label==-1):
                continue
            idx = label!=-1
            label = label[idx]
            pred = pred[idx]
            

            pred_classes = torch.max(pred, dim=1)[1]
            
            loss = loss_function(pred, label)
            
            if args.tasks[i] == 'level':
                accu_num[i] += (torch.abs(pred_classes-label) <=1).sum().item()  # torch.eq(pred_classes, label.to(device)).sum().item()
                #print('sdsa')
            else:
                
                accu_num[i] += torch.eq(pred_classes, label.to(device)).sum().item()
                # loss,error_num,error_name = calculate_loss(loss_function,pred,label,args.tasks[i],args.loss_weights[i],args.reduction,labels,cont)
                # print(loss,error_num,error_name)
                # errors_nums[i] += error_num
                # errors_lists[i].extend(error_name)

            accu_loss[i] += loss.detach().item()
            
            sens_mertics[i].update(pred_classes,label)
            spec_mertics[i].update(pred_classes,label)
            
        # s = ''.join([' loss_{}: {:.3f}, acc_{}: {:.3f}'.format(i,acc_l/ (step + 1),i,acc_n/ sample_num) for i,(acc_l,acc_n) in enumerate(zip(accu_loss,accu_num))])
        s = ''.join([' loss_{}: {:.3f}, acc_{}: {:.3f}'.format(i,accu_loss[i]/ (step + 1),i,accu_num[i]/ sample_num) for i in args.show_tasks])
        s_desc = f'[{name} epoch {epoch}] '+ s
        if cont :
            s_desc += f' error: {errors_nums}'
        data_loader.desc = s_desc
        
    sens_res = [sens.compute().item() for sens in sens_mertics]
    spec_res = [spec.compute().item() for spec in spec_mertics] 
    print(f'{name} Senstive: ',sens_res)
    print(f'{name} Specificity: ',spec_res)

    if cont:
        err_res = ''.join([f'total_errors{i} : {errors_nums[i]} {len(errors_lists[i])} ' for i in args.show_tasks])
        print(err_res)
        
    return np.array(accu_loss) / (step + 1), np.array(accu_num) / sample_num,  errors_lists




def train_one_epoch_multi_moe(model, optimizer, data_loader, device, epoch, multi_tasks,istrain=[True]*args.multi_tasks,cont=False,use_weight=False):
    model.train()
    
    # loss_function = torch.nn.BCELoss()#torch.nn.CrossEntropyLoss()
    loss_functions = args.loss_fns
    accu_loss = torch.zeros(1).to(device)  # 累计损失
    accu_num = torch.zeros(1).to(device)  # 累计预测正确的样本数
    optimizer.zero_grad()

    sample_num = 0
    data_loader = tqdm(data_loader, file=sys.stdout)
    accu_num = [0] * multi_tasks
    accu_loss = [0] * multi_tasks
    errors_nums = [0] * multi_tasks
    errors_lists = [ [] for i in range(multi_tasks) ]
    task_epoch = [1e-8] * multi_tasks
    task_num = [1e-8] * multi_tasks
    for step, data in enumerate(data_loader):
        images, labels = data
        sample_num += images.shape[0]
        x = images.to(device)
        B = x.shape[0]

        if args.backbone in ('vit','TransMIL'):
            resnet.eval()
            x = torch.reshape(x, (x.shape[0] * x.shape[1], x.shape[2], x.shape[3], x.shape[4]))
            features = pre_cls_model(x)
            features = torch.reshape(features, (B, int(features.shape[0] / B), features.shape[1]))
        elif args.backbone in ('vit_res','vit_moe'):
            features = x
        
        # task_ids= labels['task_id']
        # print(labels)
        # print(task_ids)
        preds = model(features,None)
        # print(preds.keys(),'sdww')
        # print(preds[0],preds[0].size())
        task_id = labels['task_id'].to(device)
        # index = torch.Tensor((torch.range(0,args.multi_tasks),task_id))
        task_id_mask = torch.zeros(args.multi_tasks, task_id.shape[0]).to(device)
        task_id_mask[task_id,range(task_id.shape[0])]=1
        task_id_mask = task_id_mask.long()
        # one_hot.scatter_(0,  task_id.reshape(1,-1), 1)
        # one_hot = one_hot.transpose(0,1)
        # print(task_id_mask)
        # print('--'*100)
    
        
        # print(task_id_weight,task_id_weight.size(),preds[0].size(),'wwwwwwwwwwwwwwwwwwwwwww')

        loss_total = 0
        for i in range(args.multi_tasks):
            
            # sb = task_id_mask[i].detach().cpu().sum().item()
            # print(i,sb,'ww'*50)
            if not istrain[i] or task_id_mask[i].sum().item() == 0:
                # print(f' skip {i}')
                continue

            pred = preds[i].to(device) 
            label = labels[f'label_{i}'].to(device) 
            pred = pred[task_id_mask[i]==1]
            label = label[task_id_mask[i]==1]
            
            loss_function = get_lossfn(loss_functions[i],use_reduction=True)
            loss = loss_function(pred,label)
            if args.reduction == 'none':
                loss = loss.mean()
            
            pred_classes = torch.max(torch.softmax(pred, dim=1), dim=1)[1]
            accu_num[i] += torch.eq(pred_classes, label.to(device)).sum().item()
            
            accu_loss[i] += loss.detach().item()
            task_epoch[i] += 1
            task_num[i] += task_id_mask[i].sum().item()
            
            task_num_torch = torch.Tensor(task_num)
            if use_weight and torch.all(task_num_torch>5):
                weights_p = task_num_torch / task_num_torch.sum()
                weight = 1 / (weights_p[i] / weights_p).sum()
                loss *= weight
                
            
            loss_total +=loss
        loss_noisy = collect_noisy_gating_loss(model,weight=0.01)
        loss_total+=loss_noisy
        loss_total.backward()
        # print(task_num,accu_num)
        # s = ''.join([' loss_{}: {:.3f}, acc_{}: {:.3f}'.format(i,acc_l/ (step + 1),i,acc_n/ sample_num) for i,(acc_l,acc_n) in enumerate(zip(accu_loss,accu_num))])
        s = ''.join([' loss_{}: {:.3f}, acc_{}: {:.3f}'.format(i,accu_loss[i]/ (task_epoch[i]),i,accu_num[i]/ task_num[i]) for i in args.show_tasks])
 
        s_desc = f'[train epoch {epoch}] '+ s 

        data_loader.desc = s_desc

        if not torch.isfinite(loss_total):
            print('WARNING: non-finite loss, ending training ', loss_total)
            sys.exit(1)

        optimizer.step()
        optimizer.zero_grad()
    
        #print(f' total errors :{errors_nums[1]} ,{len(errors_lists[1])}')
    return np.array(accu_loss) / np.array(task_epoch), np.array(accu_num) / np.array(task_num), errors_lists




@torch.no_grad()
def evaluate_multi_moe(model, data_loader, device, epoch, multi_tasks,name='valid',cont=False):
    loss_function = torch.nn.CrossEntropyLoss()

    model.eval()
    accu_num = torch.zeros(1).to(device)  # 累计预测正确的样本数
    accu_loss = torch.zeros(1).to(device)  # 累计损失
    zhenyin = 0
    gjb = 0
    sample_num = 0
    louzhen = 0
    data_loader = tqdm(data_loader, file=sys.stdout)
    accu_loss = [0] * multi_tasks
    accu_num = [0] * multi_tasks
    loss_functions = args.loss_fns
    errors_nums = [0] * multi_tasks
    errors_lists = [ [] for i in range(multi_tasks) ]

    for step, data in enumerate(data_loader):
        
        images, labels = data
        sample_num += images.shape[0]
        
        x = images.to(device)
        
        B = x.shape[0]
        
        if args.backbone in ('vit','TransMIL'):
            resnet.eval()
            x = torch.reshape(x, (x.shape[0] * x.shape[1], x.shape[2], x.shape[3], x.shape[4]))
            features = pre_cls_model(x)
            features = torch.reshape(features, (B, int(features.shape[0] / B), features.shape[1]))
        elif args.backbone in ('vit_res','vit_moe'):
            features = x
 

        preds = model(features,None)
              
        loss_total = 0
        for i in range(args.multi_tasks):
    
            pred = preds[i].to(device)
            label = labels[f'label_{i}'].to(device) 
           
            loss_function = get_lossfn(loss_functions[i],use_reduction=False)
            loss = loss_function(pred,label)
            if args.reduction == 'none':
                loss = loss.mean()
            
            pred_classes = torch.max(torch.softmax(pred, dim=1), dim=1)[1]
            # print(pred_classes,label,loss,pred)
            accu_num[i] += torch.eq(pred_classes, label.to(device)).sum().item()
            
            accu_loss[i] += loss.detach().item()
            loss_total +=loss
        loss_noisy = collect_noisy_gating_loss(model,weight=0.01)
        loss_total+=loss_noisy
        # print(accu_loss,accu_num)

        # s = ''.join([' loss_{}: {:.3f}, acc_{}: {:.3f}'.format(i,acc_l/ (step + 1),i,acc_n/ sample_num) for i,(acc_l,acc_n) in enumerate(zip(accu_loss,accu_num))])
        s = ''.join([' loss_{}: {:.3f}, acc_{}: {:.3f}'.format(i,accu_loss[i]/ (step + 1),i,accu_num[i]/ sample_num) for i in args.show_tasks])
        s_desc = f'[{name} epoch {epoch}] '+ s

        data_loader.desc = s_desc

        
    return np.array(accu_loss) / (step + 1), np.array(accu_num) / sample_num,  errors_lists





def train_one_epoch_multi_moe_dis(model, optimizer, data_loader, local_rank, epoch, multi_tasks,istrain=[True]*args.multi_tasks,cont=False,use_weight=False):
    model.train()
    
    # loss_function = torch.nn.BCELoss()#torch.nn.CrossEntropyLoss()
    loss_functions = args.loss_fns
    accu_loss = torch.zeros(1).to(local_rank)  # 累计损失
    accu_num = torch.zeros(1).to(local_rank)  # 累计预测正确的样本数
    optimizer.zero_grad()

    sample_num = 0
    data_loader = tqdm(data_loader, file=sys.stdout)
    accu_num = [0] * multi_tasks
    accu_loss = [0] * multi_tasks
    errors_nums = [0] * multi_tasks
    errors_lists = [ [] for i in range(multi_tasks) ]
    task_epoch = [1e-8] * multi_tasks
    task_num = [1e-8] * multi_tasks
    
    sens_mertics = [ Recall(task='multiclass' if i>2 else 'binary',num_classes=i,average='micro').cuda(local_rank) for i in args.num_classes ]
    spec_mertics = [ Specificity(task='multiclass' if i>2 else 'binary',num_classes=i,average='micro').cuda(local_rank) for i in args.num_classes ]
    
    for step, data in enumerate(data_loader):
        images, labels = data
        sample_num += images.shape[0]
        x = images.cuda(local_rank)
        B = x.shape[0]

        if args.backbone in ('vit','TransMIL'):
            resnet.eval()
            x = torch.reshape(x, (x.shape[0] * x.shape[1], x.shape[2], x.shape[3], x.shape[4]))
            features = pre_cls_model(x)
            features = torch.reshape(features, (B, int(features.shape[0] / B), features.shape[1]))
        elif args.backbone in ('vit_res','vit_moe'):
            features = x
        
        # task_ids= labels['task_id']
        # print(labels)
        # print(task_ids)
        preds = model(features)

        task_id = labels['task_id'].cuda(local_rank)
        # index = torch.Tensor((torch.range(0,args.multi_tasks),task_id))
        task_id_mask = torch.zeros(args.multi_tasks, task_id.shape[0]).cuda(local_rank)
        task_id_mask[task_id,range(task_id.shape[0])]=1
        task_id_mask = task_id_mask.long()

        loss_total = 0
        for i in range(args.multi_tasks):
            
            # sb = task_id_mask[i].detach().cpu().sum().item()
            # print(i,sb,'ww'*50)
            if not istrain[i] or task_id_mask[i].sum().item() == 0:
                # print(f' skip {i}')
                continue

            pred = preds[i].cuda(local_rank)
            label = labels[f'label_{i}'].cuda(local_rank) 
            pred = pred[task_id_mask[i]==1]
            label = label[task_id_mask[i]==1]
            
            loss_function = get_lossfn(loss_functions[i],use_reduction=True)
            loss = loss_function(pred,label)
            if args.reduction == 'none':
                loss = loss.mean()
            
            pred_classes = torch.max(torch.softmax(pred, dim=1), dim=1)[1]
            accu_num[i] += torch.eq(pred_classes, label.cuda(local_rank)).sum().item()
            
            accu_loss[i] += loss.detach().item()
            task_epoch[i] += 1
            task_num[i] += task_id_mask[i].sum().item()

            sens_mertics[i].update(pred_classes,label)
            spec_mertics[i].update(pred_classes,label)
            
            task_num_torch = torch.Tensor(task_num)
            if use_weight and torch.all(task_num_torch>5):
                weights_p = task_num_torch / task_num_torch.sum()
                weight = 1 / (weights_p[i] / weights_p).sum()
                loss *= weight
                # if local_rank == 0:
                #     print(i,weight)
                
            loss_total +=loss
        loss_noisy = collect_noisy_gating_loss(model,weight=0.01)
        loss_total+=loss_noisy
        loss_total.backward()
        # print(task_num,accu_num)
        # s = ''.join([' loss_{}: {:.3f}, acc_{}: {:.3f}'.format(i,acc_l/ (step + 1),i,acc_n/ sample_num) for i,(acc_l,acc_n) in enumerate(zip(accu_loss,accu_num))])
        s = ''.join([' loss_{}: {:.3f}, acc_{}: {:.3f}'.format(i,accu_loss[i]/ (task_epoch[i]),i,accu_num[i]/ task_num[i]) for i in args.show_tasks])
 
        s_desc = f'[train epoch {epoch}] '+ s 

        data_loader.desc = s_desc

        if not torch.isfinite(loss_total):
            print('WARNING: non-finite loss, ending training ', loss_total)
            sys.exit(1)

        optimizer.step()
        optimizer.zero_grad()
    
        #print(f' total errors :{errors_nums[1]} ,{len(errors_lists[1])}')

    sens_res = [sens.compute().item() for sens in sens_mertics]
    spec_res = [spec.compute().item() for spec in spec_mertics] 
    print('Train Senstive: ',sens_res)
    print('Train Specificity: ',spec_res)
    return np.array(accu_loss) / np.array(task_epoch), np.array(accu_num) / np.array(task_num), errors_lists, sens_res, spec_res




@torch.no_grad()
def evaluate_multi_moe_dis(model, data_loader, local_rank, epoch, multi_tasks,name='valid',cont=False):
    loss_function = torch.nn.CrossEntropyLoss()

    model.eval()

    zhenyin = 0
    gjb = 0
    sample_num = 0
    louzhen = 0
    data_loader = tqdm(data_loader, file=sys.stdout)
    accu_loss = [0] * multi_tasks
    accu_num = [0] * multi_tasks
    loss_functions = args.loss_fns
    errors_nums = [0] * multi_tasks
    errors_lists = [ [] for i in range(multi_tasks) ]
    
    sens_mertics = [ Recall(task='multiclass' if i>2 else 'binary',num_classes=i,average='micro').cuda(local_rank) for i in args.num_classes ]
    spec_mertics = [ Specificity(task='multiclass' if i>2 else 'binary',num_classes=i,average='micro').cuda(local_rank) for i in args.num_classes ]
    auc_mertics =  [ AUROC(task='multiclass' if i>2 else 'binary',num_classes=i,average='micro').cuda(local_rank) for i in args.num_classes ]

    for step, data in enumerate(data_loader):
        
        images, labels = data
        sample_num += images.shape[0]
        
        x = images.cuda(local_rank)
        
        B = x.shape[0]
        
        if args.backbone in ('vit','TransMIL'):
            resnet.eval()
            x = torch.reshape(x, (x.shape[0] * x.shape[1], x.shape[2], x.shape[3], x.shape[4]))
            features = pre_cls_model(x)
            features = torch.reshape(features, (B, int(features.shape[0] / B), features.shape[1]))
        elif args.backbone in ('vit_res','vit_moe'):
            features = x
 

        preds = model(features)
        code = labels['code']
        loss_total = 0
        for i,key in enumerate(list(labels.keys())[:multi_tasks]):
    
            pred = preds[i].cuda(local_rank)
            label = labels[key].cuda(local_rank) 
            if torch.all(label==-1):
                continue
            idx = label!=-1
            label = label[idx]
            pred = pred[idx]
            highlabel = labels['highlabel'].cuda(local_rank)
            highlabel = highlabel[idx]
            
            code1 = [code[k] for k in range(len(code)) if idx[k]]
            # print(len(code1),highlabel.size(),pred.size(),'ssssssssssssssssss')
            labels1 = {'highlabel':highlabel,'code':code1}
            loss,errors_num,error_name = calculate_loss(loss_function,pred,label,args.tasks[i],args.loss_weights[i],args.reduction,labels1,cont)
            accu_loss[i] += loss.detach().item()
            errors_nums[i] += errors_num
            errors_lists[i].extend(error_name)
            
            loss_function = get_lossfn(loss_functions[i],use_reduction=False)
            loss = loss_function(pred,label)
            if args.reduction == 'none':
                loss = loss.mean()
            
            pred_classes = torch.max(torch.softmax(pred, dim=1), dim=1)[1]
            # print(i,pred_classes,label.cpu().numpy(),labels['code'])
            # print(pred_classes,label,loss,pred)
            accu_num[i] += torch.eq(pred_classes, label).sum().item()
            
            # accu_loss[i] += loss.detach().item()
            loss_total +=loss
            
            sens_mertics[i].update(pred_classes,label)
            spec_mertics[i].update(pred_classes,label)
            auc_mertics[i].update(pred_classes,label)
            
        loss_noisy = collect_noisy_gating_loss(model,weight=0.01)
        loss_total+=loss_noisy
        # print(accu_loss,accu_num)

        # s = ''.join([' loss_{}: {:.3f}, acc_{}: {:.3f}'.format(i,acc_l/ (step + 1),i,acc_n/ sample_num) for i,(acc_l,acc_n) in enumerate(zip(accu_loss,accu_num))])
        
        s = ''.join([' loss_{}: {:.3f}, acc_{}: {:.3f}'.format(i,accu_loss[i]/ (step + 1),i,accu_num[i]/ sample_num) for i in args.show_tasks])
        s_desc = f'[{name} epoch {epoch}] '+ s
        
        if cont :
            s_desc += f' error: {errors_nums[0]}'

        data_loader.desc = s_desc

    sens_res = [sens.compute().item() for sens in sens_mertics]
    spec_res = [spec.compute().item() for spec in spec_mertics] 
    auc_res = [auc.compute().item() for auc in auc_mertics] 
    
    if cont:
        # err_res = ''.join([f'total_errors{i} : {errors_nums[i]} {len(errors_lists[i])} ' for i in args.show_tasks])
        # print(err_res)
        print(f'total_errors_label : {errors_nums[0]} {len(errors_lists[0])} ')
        
    print(f'{name} Senstive: ',sens_res)
    print(f'{name} Specificity: ',spec_res)
    print(f'{name} AUC: ',auc_res)
    return np.array(accu_loss) / (step + 1), np.array(accu_num) / sample_num,  errors_lists, sens_res, spec_res,auc_res



def collect_noisy_gating_loss(model, weight):
    loss = 0
    for module in model.modules():
        if (isinstance(module, NoisyGate) or isinstance(module, NoisyGate_VMoE)) and module.has_loss:
            # print(module)
            loss += module.get_loss()
    return loss * weight





def train_one_epoch_multi_moe_dis2(model, optimizer, data_loader, local_rank, epoch, multi_tasks,istrain=[True]*args.multi_tasks,cont=False,use_weight=False):
    model.train()
    
    # loss_function = torch.nn.BCELoss()#torch.nn.CrossEntropyLoss()
    loss_functions = args.loss_fns
    accu_loss = torch.zeros(1).to(local_rank)  # 累计损失
    accu_num = torch.zeros(1).to(local_rank)  # 累计预测正确的样本数
    optimizer.zero_grad()

    sample_num = 0
    data_loader = tqdm(data_loader, file=sys.stdout)
    accu_num = [0] * multi_tasks
    accu_loss = [0] * multi_tasks
    errors_nums = [0] * multi_tasks
    errors_lists = [ [] for i in range(multi_tasks) ]
    task_epoch = [1e-8] * multi_tasks
    task_num = [1e-8] * multi_tasks
    
    sens_mertics = [ Recall(task='multiclass' if i>2 else 'binary',num_classes=i,average='micro').cuda(local_rank) for i in args.num_classes ]
    spec_mertics = [ Specificity(task='multiclass' if i>2 else 'binary',num_classes=i,average='micro').cuda(local_rank) for i in args.num_classes ]
    auc_mertics =  [ AUROC(task='multiclass' if i>2 else 'binary',num_classes=i,average='micro').cuda(local_rank) for i in args.num_classes ]

    
    for step, data in enumerate(data_loader):
        images, labels = data
        sample_num += images.shape[0]
        x = images.cuda(local_rank)
        B = x.shape[0]

        if args.backbone in ('vit','TransMIL'):
            resnet.eval()
            x = torch.reshape(x, (x.shape[0] * x.shape[1], x.shape[2], x.shape[3], x.shape[4]))
            features = pre_cls_model(x)
            features = torch.reshape(features, (B, int(features.shape[0] / B), features.shape[1]))
        elif args.backbone in ('vit_res','vit_moe'):
            features = x
        # print(labels.keys())
        # task_ids= labels['task_id']
        # print(labels)
        # print(task_ids)
        # print('aaaaaaaaaa',step)
        preds = model(features)
        # print('cccccccccccc')
        # 
        
        task_id = labels['task_id'].cuda(local_rank)
        # index = torch.Tensor((torch.range(0,args.multi_tasks),task_id))
        task_id_mask = torch.zeros(args.multi_tasks, task_id.shape[0]).cuda(local_rank)
        task_id_mask[task_id,range(task_id.shape[0])]=1
        task_id_mask = task_id_mask.long()
        

        code = labels['code']
        loss_total = 0
        for i,key in enumerate(list(labels.keys())[:args.multi_tasks]):
            # print(key)
            # print('bbbbbbbbbbbbbbbbbbbb',args.tasks[i])
            
            # sb = task_id_mask[i].detach().cpu().sum().item()
            # print(i,sb,'ww'*50)
            if not istrain[i] or task_id_mask[i].sum().item() == 0:
                # print(f' skip {i}')
                continue

            pred = preds[i].cuda(local_rank)
            label = labels[key].cuda(local_rank) 
            highlabel = labels['highlabel'].cuda(local_rank)
            
            # print(highlabel.size(),task_id_mask[i].size(),pred.size(),'dsd')
            pred = pred[task_id_mask[i]==1]
            label = label[task_id_mask[i]==1]
            highlabel = highlabel[task_id_mask[i]==1]
            # labels['code'] = labels['code']
            
            code1 = [code[k] for k in range(len(code)) if task_id_mask[i][k] == 1]
            # print(args.tasks[i],type(labels['code']),len(code1),pred.size(),highlabel.size())
            
            loss_function = get_lossfn(loss_functions[i],use_reduction=True)

            labels1 = {'highlabel':highlabel,'code':code1}
            loss,errors_num,error_name = calculate_loss(loss_function,pred,label,args.tasks[i],args.loss_weights[i],args.reduction,labels1,cont)
            accu_loss[i] += loss.detach().item()
            errors_nums[i] += errors_num
            errors_lists[i].extend(error_name)
             
            pred_classes = torch.max(torch.softmax(pred, dim=1), dim=1)[1]
            accu_num[i] += torch.eq(pred_classes, label.cuda(local_rank)).sum().item()

            task_epoch[i] += 1
            task_num[i] += task_id_mask[i].sum().item()

            sens_mertics[i].update(pred_classes,label)
            spec_mertics[i].update(pred_classes,label)
            auc_mertics[i].update(pred_classes,label)
            
            task_num_torch = torch.Tensor(task_num)
            if use_weight and torch.all(task_num_torch>5):
                weights_p = task_num_torch / task_num_torch.sum()
                weight = 1 / (weights_p[i] / weights_p).sum()
                loss *= weight
                # if local_rank == 0:
                #     print(i,weight)
                
            loss_total +=loss
        loss_noisy = collect_noisy_gating_loss(model,weight=0.01)
        loss_total+=loss_noisy
        loss_total.backward()
        # print(task_num,accu_num)
        # s = ''.join([' loss_{}: {:.3f}, acc_{}: {:.3f}'.format(i,acc_l/ (step + 1),i,acc_n/ sample_num) for i,(acc_l,acc_n) in enumerate(zip(accu_loss,accu_num))])
        s = ''.join([' loss_{}: {:.3f}, acc_{}: {:.3f}'.format(i,accu_loss[i]/ (task_epoch[i]),i,accu_num[i]/ task_num[i]) for i in args.show_tasks])
 
        s_desc = f'[train epoch {epoch}] '+ s 
        
        if cont :
            s_desc += f' error: {errors_nums[0]}'
        data_loader.desc = s_desc
        
        


        if not torch.isfinite(loss_total):
            print('WARNING: non-finite loss, ending training ', loss_total)
            sys.exit(1)

        optimizer.step()
        optimizer.zero_grad()
    
        #print(f' total errors :{errors_nums[1]} ,{len(errors_lists[1])}')

    sens_res = [sens.compute().item() for sens in sens_mertics]
    spec_res = [spec.compute().item() for spec in spec_mertics] 
    

    # print(local_rank)

    auc_res = [auc.compute().item() for auc in auc_mertics] 
    
    if cont:
        # err_res = ''.join([f'total_errors{i} : {errors_nums[i]} {len(errors_lists[i])} ' for i in args.show_tasks])
        print(f'total_errors_label : {errors_nums[0]} {len(errors_lists[0])} ')
        
    print('Train Sensitive: ',sens_res)
    print('Train Specificity: ',spec_res)
    print('Train AUC: ',auc_res)
    return np.array(accu_loss) / np.array(task_epoch), np.array(accu_num) / np.array(task_num), errors_lists, sens_res, spec_res, auc_res