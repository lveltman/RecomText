import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import torch
import torch.nn.functional as F
from tqdm import tqdm
from utils.losses import get_losses
from utils.metrics import MetricsCalculator
import pandas as pd
import numpy as np
import faiss

class Trainer:
    def __init__(self, model, train_loader, val_loader, optimizer, config):
        self.model = model
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.optimizer = optimizer
        self.config = config
        
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model.to(self.device)
        
        self.contrastive_loss_fn, self.recommendation_loss_fn = get_losses()
        self.metrics_calculator = MetricsCalculator()
        self.ks = config.get('evaluation', {}).get('ks', [1, 5, 10])
        
        self.best_metric = float('-inf')
        self.best_epoch = 0
        self.patience = config.get('training', {}).get('patience', 5)
        self.no_improvement = 0

    def train(self, epochs):
        """Полный цикл обучения с валидацией."""
        for epoch in range(epochs):
            print(f"\nEpoch {epoch + 1}/{epochs}")
            
            # Обучение
            train_metrics = self.train_epoch()
            if epoch == 0:
                self._save_checkpoint(epoch)
            print("\nTraining metrics:")
            self._print_metrics(train_metrics)
            
            # Валидация
            val_metrics = self.validate()
            if val_metrics:  # Проверяем, что метрики не None
                # Проверка на улучшение
                current_metric = val_metrics.get('contextual_ndcg', 0)  # Используем contextual_ndcg
                if current_metric > self.best_metric:
                    self.best_metric = current_metric
                    self.best_epoch = epoch
                    self.no_improvement = 0
                    self._save_checkpoint(epoch, val_metrics)
                else:
                    self.no_improvement += 1
                
                # Early stopping
                if self.no_improvement >= self.patience:
                    print(f"\nEarly stopping triggered. No improvement for {self.patience} epochs.")
                    break

    def train_epoch(self):
        """Один эпох обучения."""
        self.model.train()
        total_loss = 0
        total_contrastive_loss = 0
        total_recommendation_loss = 0

        for batch in tqdm(self.train_loader, desc="Training"):
            loss, c_loss, r_loss = self.training_step(batch)
            
            total_loss += loss
            total_contrastive_loss += c_loss
            total_recommendation_loss += r_loss

        return {
            'loss': total_loss / len(self.train_loader),
            'contrastive_loss': total_contrastive_loss / len(self.train_loader),
            'recommendation_loss': total_recommendation_loss / len(self.train_loader)
        }

    def validate(self):
        """Валидация модели с использованием актуального FAISS индекса"""
        self.model.eval()
        total_loss = 0
        total_contrastive_loss = 0
        total_recommendation_loss = 0
        
        # Загрузка необходимых данных
        textual_history = pd.read_parquet('./data/textual_history.parquet')
        df_videos = pd.read_parquet("./data/video_info.parquet")
        df_videos_map = df_videos.set_index('clean_video_id').to_dict(orient='index')

        # Проверка и обновление индекса
        index_path = self.config['inference']['index_path']
        ids_path = self.config['inference']['ids_path']
        embeddings_path = self.config['inference']['embeddings_path']
        
        if not all(os.path.exists(p) for p in [index_path, ids_path, embeddings_path]):
            print("\nIndex files not found, creating new index...")
            try:
                from indexer import main as create_index
                create_index(config=self.config)
            except Exception as e:
                print(f"Error creating index: {str(e)}")
                return None

        # Загрузка индекса и данных
        index = faiss.read_index(index_path)
        video_ids = np.load(ids_path)
        item_embeddings_array = np.load(embeddings_path)
        
        # Загрузка демографических данных
        try:
            demographic_data = pd.read_parquet('./data/demographic_data.parquet')
            demographic_features = ['age_group', 'sex', 'region']
            
            # Создаем центроиды для каждой демографической группы
            demographic_centroids = {}
            for feature in demographic_features:
                demographic_centroids[feature] = {}
                for group in demographic_data[feature].unique():
                    # Получаем пользователей из этой группы
                    group_users = demographic_data[demographic_data[feature] == group]['viewer_uid'].values
                    
                    # Получаем их эмбеддинги из истории просмотров
                    group_embeddings = []
                    for user_id in group_users:
                        if user_id in textual_history['viewer_uid'].values:
                            user_idx = textual_history[textual_history['viewer_uid'] == user_id].index[0]
                            if user_idx < len(item_embeddings_array):
                                group_embeddings.append(item_embeddings_array[user_idx])
                    
                    if group_embeddings:
                        # Вычисляем центроид группы
                        group_centroid = np.mean(group_embeddings, axis=0)
                        demographic_centroids[feature][group] = torch.tensor(
                            group_centroid, 
                            device=self.device
                        )
            
            print(f"Loaded demographic data with features: {demographic_features}")
        except Exception as e:
            print(f"Warning: Could not load demographic data: {str(e)}")
            demographic_centroids = None
        
        # Инициализация метрик
        metrics_calculator = MetricsCalculator(sim_threshold=0.7)
        metrics_accum = {metric: 0.0 for metric in ["semantic_precision@k", "cross_category_relevance", "contextual_ndcg"]}
        num_users = 0
        top_k = self.config['inference'].get('top_k', 10)

        with torch.no_grad():
            for batch_idx, batch in enumerate(tqdm(self.val_loader, desc="Validation")):
                # Обработка батча
                items_text_inputs, user_text_inputs, item_ids, user_ids = [
                    self.to_device(x) for x in batch
                ]
                
                # Forward pass
                items_embeddings, user_embeddings = self.model(
                    items_text_inputs, user_text_inputs, item_ids, user_ids
                )

                # Нормализация эмбеддингов
                items_embeddings = F.normalize(items_embeddings, p=2, dim=1)
                user_embeddings = F.normalize(user_embeddings, p=2, dim=1)

                # Расчет потерь
                rec_loss = self.compute_recommendation_loss(user_embeddings, items_embeddings)
                con_loss = self.compute_contrastive_loss(items_embeddings, user_embeddings)
                total_loss += (con_loss + self.config['training']['lambda_rec'] * rec_loss).item()
                
                # Поиск рекомендаций и расчет метрик
                for i in range(user_embeddings.size(0)):
                    user_metrics = self._process_user(
                        user_embeddings[i], 
                        items_embeddings[i], 
                        item_ids[i], 
                        user_ids[i], 
                        index, 
                        video_ids, 
                        df_videos_map,
                        item_embeddings_array,
                        metrics_calculator,
                        top_k,
                        demographic_data,
                        demographic_centroids
                    )
                    self._update_metrics(metrics_accum, user_metrics)
                    num_users += 1

        return self._compile_metrics(total_loss, total_contrastive_loss, total_recommendation_loss, metrics_accum, num_users)

    def _process_user(self, user_emb, target_emb, item_id, user_id, index, video_ids, df_videos_map, item_embeddings, metrics_calculator, top_k, demographic_data, demographic_centroids):
        """Обработка одного пользователя для расчета метрик"""
        # Поиск рекомендаций
        user_emb_np = user_emb.cpu().numpy().astype('float32')
        distances, indices = index.search(user_emb_np.reshape(1, -1), top_k)
        
        # Получение рекомендаций
        rec_embeddings = torch.tensor(item_embeddings[indices[0]], device=self.device)
        rec_embeddings = F.normalize(rec_embeddings, p=2, dim=1)
        
        # Метаданные рекомендаций
        rec_categories = []
        for idx in indices[0]:
            video_id = str(video_ids[idx])
            rec_categories.append(df_videos_map.get(video_id, {}).get('category', 'Unknown'))

        # Демографические данные
        user_demo = {}
        if demographic_data is not None and user_id.item() in demographic_data.index:
            for feature in ['age_group', 'sex', 'region']:
                user_demo[feature] = demographic_data.loc[user_id.item(), feature]

        # Целевой товар
        target_id = str(item_id.item())
        target_category = df_videos_map.get(target_id, {}).get('category', 'Unknown')

        # Добавляем демографические данные если они доступны
        if demographic_centroids is not None:
            user_demographics = {}
            user_id = self.val_dataset.reverse_user_id_map[user_ids[i].item()]
            user_demo = demographic_data[demographic_data['viewer_uid'] == user_id].iloc[0]
            
            for feature in demographic_features:
                user_demographics[feature] = user_demo[feature]
            
            # Добавляем DAS метрики
            das_metrics = metrics_calculator.demographic_alignment_score(
                user_demographics,
                rec_embeddings,
                demographic_centroids
            )
            user_metrics = das_metrics

        return user_metrics

    def _update_metrics(self, metrics_accum, user_metrics):
        """Обновление аккумуляторов метрик"""
        for metric, value in user_metrics.items():
            if metric not in metrics_accum:
                metrics_accum[metric] = 0.0
            metrics_accum[metric] += value

    def _compile_metrics(self, total_loss, contrastive_loss, recommendation_loss, metrics_accum, num_users):
        """Компиляция финальных метрик."""
        metrics_dict = {
            'val_loss': total_loss / len(self.val_loader),
            'val_contrastive_loss': contrastive_loss / len(self.val_loader),
            'val_recommendation_loss': recommendation_loss / len(self.val_loader)
        }
        
        if num_users > 0:
            # Добавляем все накопленные метрики
            for metric, total_value in metrics_accum.items():
                metrics_dict[metric] = total_value / num_users
                
        # Используем новую функцию для вывода метрик
        print("\nValidation Metrics:")
        self._print_metrics(metrics_dict)
            
        return metrics_dict

    def _save_checkpoint(self, epoch, metrics=None):
        """Улучшенное сохранение чекпоинта"""
        checkpoint_dir = self.config['training']['checkpoint_dir']
        os.makedirs(checkpoint_dir, exist_ok=True)
        
        # Сохранение модели
        model_path = os.path.join(checkpoint_dir, f'model_epoch_{epoch}')
        self.model.save_pretrained(model_path)
        
        # Обновление конфига для индексатора
        index_config = self.config.copy()
        index_config['inference']['model_path'] = model_path
        
        try:
            from indexer import main as update_index
            update_index(config=index_config)
            print("FAISS index updated successfully")
        except Exception as e:
            print(f"Error updating index: {str(e)}")

    def _print_metrics(self, metrics):
        """Форматированный вывод метрик по группам."""
        
        # Группируем метрики по типам
        groups = {
            'Losses': {k: v for k, v in metrics.items() if 'loss' in k.lower()},
            'Semantic Metrics': {k: v for k, v in metrics.items() if 'semantic' in k.lower()},
            'Category Metrics': {k: v for k, v in metrics.items() if 'category' in k.lower() or 'cross' in k.lower()},
            'NDCG': {k: v for k, v in metrics.items() if 'ndcg' in k.lower()},
            'Demographic Alignment': {k: v for k, v in metrics.items() if 'das_' in k.lower()}
        }
        
        # Выводим метрики по группам
        for group_name, group_metrics in groups.items():
            if group_metrics:  # Выводим группу только если есть метрики
                print(f"\n{group_name}:")
                for name, value in group_metrics.items():
                    if isinstance(value, (int, float)):
                        print(f"  {name}: {value:.4f}")
                    else:
                        print(f"  {name}: {value}")

    def training_step(self, batch):
        """Один шаг обучения."""
        items_text_inputs, user_text_inputs, item_ids, user_ids = [
            self.to_device(x) for x in batch
        ]

        # Forward pass
        items_embeddings, user_embeddings = self.model(
            items_text_inputs, user_text_inputs, item_ids, user_ids
        )

        # Нормализация эмбеддингов
        items_embeddings = F.normalize(items_embeddings, p=2, dim=1)
        user_embeddings = F.normalize(user_embeddings, p=2, dim=1)

        # Потери
        recommendation_loss = self.compute_recommendation_loss(
            user_embeddings, items_embeddings
        )
        contrastive_loss = self.compute_contrastive_loss(
            items_embeddings, user_embeddings
        )

        # Общая потеря
        loss = contrastive_loss + self.config['training']['lambda_rec'] * recommendation_loss

        # Backpropagation
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        return loss.item(), contrastive_loss.item(), recommendation_loss.item()

    def compute_recommendation_loss(self, user_embeddings, items_embeddings):
        """Вычисление потери рекомендаций."""
        logits = torch.matmul(user_embeddings, items_embeddings.T)
        labels = torch.arange(len(user_embeddings)).to(self.device)
        return self.recommendation_loss_fn(logits, labels)

    def compute_contrastive_loss(self, items_embeddings, user_embeddings):
        """Вычисление контрастивной потери."""
        batch_size = items_embeddings.size(0)
        positive_labels = torch.ones(batch_size, device=self.device)
        
        contrastive_goods_loss = self.contrastive_loss_fn(
            items_embeddings,
            items_embeddings.roll(shifts=1, dims=0),
            positive_labels
        )
        
        contrastive_users_loss = self.contrastive_loss_fn(
            user_embeddings,
            user_embeddings.roll(shifts=1, dims=0),
            positive_labels
        )
        
        return contrastive_goods_loss + contrastive_users_loss

    def to_device(self, x):
        """Перемещение данных на устройство."""
        if isinstance(x, dict):
            return {k: v.to(self.device) for k, v in x.items()}
        return x.to(self.device) 