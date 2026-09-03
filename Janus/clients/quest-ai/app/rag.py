import chromadb
from chromadb.utils import embedding_functions
from app.config import settings

COLLECTION_NAME = "lore"

_embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
    model_name="intfloat/multilingual-e5-base"
)


def get_collection():
    client = chromadb.PersistentClient(path=settings.CHROMA_PATH)
    return client.get_or_create_collection(
        name=COLLECTION_NAME, embedding_function=_embedding_fn
    )


class LoreRetriever:
    def __init__(self):
        self.collection = get_collection()

    def retrieve(self, query: str, current_stage: int, top_k: int = None) -> list[str]:
        """Возвращает релевантные куски лора, ИСКЛЮЧАЯ материалы будущих этапов.

        Ключевая идея: у каждого чанка в metadata есть "stage" — номер этапа,
        на котором информация становится доступна. Мы жёстко фильтруем на
        уровне запроса к базе, а не полагаемся на то, что модель сама
        "не подсмотрит" в системный промпт.
        """
        top_k = top_k or settings.TOP_K_CHUNKS
        results = self.collection.query(
            query_texts=[f"query: {query}"],
            n_results=top_k,
            where={"stage": {"$lte": current_stage}},
        )
        docs = results.get("documents", [[]])[0]
        return docs


retriever = LoreRetriever()
