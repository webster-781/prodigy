# A bit improved SingleLayerGeneralGNN.
# It supports an arbitrary order of different internal layers (the metagraph GNN, the background GNN, two supernode pooling-related layers)
# For possible layers, see models/layer_classes.py

import torch
import torch_geometric as pyg
import numpy as np
from models.layer_classes import MetagraphLayer, SupernodeAggrLayer, SupernodeToBgGraphLayer, BackgroundGNNLayer


class SingleLayerGeneralGNN(torch.nn.Module):
    def __init__(self, layer_list, initial_label_mlp=torch.nn.Identity(), initial_input_mlp=torch.nn.Identity(),
                 final_label_mlp=torch.nn.Identity(), final_input_mlp=torch.nn.Identity(),
                 params=None, text_dropout=None):
        super().__init__()
        self.layer_list = layer_list
        self.cos = torch.nn.CosineSimilarity(dim=1)
        self.initial_label_mlp = initial_label_mlp
        self.initial_input_mlp = initial_input_mlp  # project labels first (they might, for example,
        # have different shape at the begining than the embedding space)

        self.final_label_mlp = final_label_mlp
        self.final_input_mlp = final_input_mlp
        self.learned_label_embedding = torch.nn.Embedding(1000, params["emb_dim"])

        if params is not None:
            self.params = params
        self.logit_scale = torch.nn.Parameter(torch.ones([]) * np.log(1 / 0.07))
        self.txt_dropout = text_dropout

    def decode(self, input_x, label_x, metagraph_edge_index, edgelist_bipartite=False):
        '''
        :param input_x: As returned by the forward() method.
        :param label_x: As returned by the forward() method.
        :param edgelist_bipartite: Whether edgelist is bipartite, i.e. both left and right side are numbered from 0.
        :return:
        '''
        if edgelist_bipartite:
            ind0 = metagraph_edge_index[0, :]
            ind1 = metagraph_edge_index[1, :]
            decoded_logits = (self.cos(input_x[ind0], label_x[ind1]) + 1) / 2
            return decoded_logits
        x = torch.cat((input_x, label_x))
        ind0 = metagraph_edge_index[0, :]
        ind1 = metagraph_edge_index[1, :]
        decoded_logits = self.cos(x[ind0], x[ind1]) * self.logit_scale.exp()
        return decoded_logits


    def forward_metagraph(self, module, supernode_x, label_x, metagraph_edge_index, metagraph_edge_attr, query_set_mask, input_seqs, query_seqs, query_seqs_gt):
        '''
        Forward pass on the graph embedding <-> task bipartite metagraph.
        supernode_x: output from forward1 - matrix of pooled (sub)graph embeddings.
        label_x: matrix of label embeddings - either generated by BERT (previous step) or output from previous
                 SingleLayerGeneralGNN
        metagraph_edge_index: edge_index of a directed bipartite graph mapping class embedding index to class idx.
                              Both left and right side start counting node idx from 0!
        metagraph_edge_attr: edge_attr of the metagraph
        :return: Updated pooled embeddings and task embeddings of the bipartite graph.
        '''


        x = torch.cat((supernode_x, label_x))
        if not self.params['zero_shot']:
            x = module(x=x, edge_index=metagraph_edge_index, edge_attr=metagraph_edge_attr,
                       query_mask=query_set_mask, start_right=supernode_x.shape[0], input_seqs= input_seqs, query_seqs = query_seqs, query_seqs_gt=query_seqs_gt)
        input_x_mg = x[:supernode_x.shape[0]]
        label_x_mg = x[supernode_x.shape[0]:]

        assert len(input_x_mg) == len(supernode_x)
        assert len(label_x_mg) == len(label_x)

        return input_x_mg, label_x_mg

    def forward(self, graph, x_label, y_true_matrix, metagraph_edge_index, metagraph_edge_attr, query_set_mask, input_seqs=None, query_seqs=None, query_seqs_gt=None, task_mask=None):
        '''
        Params as returned by the batching function.
        # task_mask: Not actually needed here, but is passed here from the dataloader batch output..
        :return: y_true_matrix, y_pred_matrix (for the query set only!)
        '''
        supernode_idx = graph.supernode + graph.ptr[:-1]
        breakpoint()
        #center_nodes = torch.zeros([graph.x.shape[0], 1]).to(graph.x.device)
        #center_nodes[graph.ptr[:-1]] = 1
        #graph.x = self.initial_input_mlp(torch.concat([graph.x, center_nodes], dim = 1))
        graph.x = self.initial_input_mlp(graph.x)

        if self.txt_dropout is not None:
            graph.x = self.txt_dropout(graph.x)
            if "edge_attr" in graph and graph.edge_attr is not None:
                graph.edge_attr = self.txt_dropout(graph.edge_attr)

        x_orig = graph.x.clone()

        x_label = self.initial_label_mlp(x_label)
        if self.params["ignore_label_embeddings"]:
            #x_label = torch.zeros_like(x_label).float()  # to make sure no language information is passed through the model
            # x_label = torch.nn.ReLU()(torch.normal(0, 6,x_label.shape).to(x_label.device))
            x_label = self.learned_label_embedding(torch.arange(x_label.shape[0]).to(x_label.device))
        if self.params["zero_label_embeddings"]:
            x_label = torch.zeros_like(x_label).float()
        '''
        # temporary code for debugging
        bg_gnn = self.layer_list[0]
        supernode_aggr = self.layer_list[1]
        meta_gnn = self.layer_list[2]

        node_embs = bg_gnn(graph.x, graph.edge_index, graph.edge_attr)
        supernode_x = supernode_aggr(node_embs, graph.edge_index_supernode, supernode_idx)
        x_input, x_label = self.forward_metagraph(meta_gnn, supernode_x, x_label, metagraph_edge_index, metagraph_edge_attr)
        '''
        
        x_input = torch.zeros((len(supernode_idx), x_label.size(1))).float().to(x_label.device)
        #x_input = None
        for module in self.layer_list:
            if isinstance(module, MetagraphLayer):
                if x_input is None:
                    raise Exception('MetagraphLayer must be preceded by a layer that produces supernode embeddings!')
                x_input, new_x_label = self.forward_metagraph(module, x_input, x_label, metagraph_edge_index, metagraph_edge_attr, query_set_mask, input_seqs, query_seqs, query_seqs_gt)
                if self.params["skip_path"]:
                    x_label = x_label + new_x_label
                else:
                    x_label = new_x_label
            elif isinstance(module, SupernodeAggrLayer):
                x_input = module.forward(graph.x, graph.edge_index_supernode, supernode_idx, graph.batch)
                #  This is needed to allow backpropagation - but it is perhaps not the best solution:
                graph.x = graph.x.clone()
                graph.x[supernode_idx] = x_input
            elif isinstance(module, SupernodeToBgGraphLayer):
                if x_input is None:
                    raise Exception("SupernodeToBgGraphLayer must be preceded by MetagraphLayer!")
                new_graph_x = module.forward(graph.x, x_input, graph.edge_index_supernode, supernode_idx, graph.batch)
                if self.params["skip_path"]:
                    graph.x = graph.x + new_graph_x
                else:
                    graph.x = new_graph_x
            elif isinstance(module, BackgroundGNNLayer):
                new_graph_x = module.forward(x_orig, graph.x, graph.edge_index.long(),
                                             graph.edge_attr if "edge_attr" in graph else None,
                                             graph.edge_index_supernode, graph.ptr[:-1], graph.batch)
                if self.params["skip_path"] and new_graph_x.shape == graph.x.shape:
                    graph.x = graph.x + new_graph_x
                else:
                    graph.x = new_graph_x
            else:
                raise ValueError('Unknown layer type: {}'.format(type(module)))


        x_input = self.final_input_mlp(x_input)
        x_label = self.final_label_mlp(x_label)

        y_pred_matrix = self.decode(x_input, x_label, metagraph_edge_index, edgelist_bipartite=False).reshape(
            y_true_matrix.shape)

        qry_idx = torch.where(query_set_mask.reshape(-1, y_true_matrix.shape[1])[:, 0] == 1)[0]
        return y_true_matrix[qry_idx, :], y_pred_matrix[qry_idx, :], graph

