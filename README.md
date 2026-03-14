# MD Scrapper

Modulo responsavel por integrar provedores externos de leitura/download com a fila separada do MD Scrapper.

Hoje ele cobre:

- descoberta automatica de provedores em `Providers/`
- busca de obras por nome
- resolucao de projeto por URL
- carregamento de lista de capitulos
- envio de capitulos para a tabela `scraper_downloads`
- despacho do job para o runner correto do provedor
- configuracao por provedor (`enabled`, concorrencia, pasta de destino, retries)

## Estrutura

```text
app/Services/MD_Scrapper/
|-- __init__.py
|-- registry.py
|-- runner.py
|-- settings.py
`-- Providers/
    |-- geass_comics/
    |   |-- __version__.py
    |   |-- core.py
    |   |-- provider.py
    |   `-- runner.py
    |-- manhastro/
    |   |-- __version__.py
    |   |-- core.py
    |   |-- provider.py
    |   `-- runner.py
    `-- hanami/
        |-- __version__.py
        |-- core.py
        |-- provider.py
        `-- runner.py
```

Arquivos principais:

- `registry.py`: carrega e valida `PROVIDER_DEFINITION` de cada pasta em `Providers/`
- `runner.py`: recebe um job da fila, detecta o `provider` e chama o runner especifico
- `settings.py`: resolve configuracoes do MD Scrapper e dos provedores
- `Providers/<provider>/core.py`: busca, detalhe de projeto e manifesto do capitulo
- `Providers/<provider>/runner.py`: executa o download real do capitulo
- `Providers/<provider>/provider.py`: ponto de registro do provedor

## Fluxo geral

1. A pagina `/scraper` consulta os provedores disponiveis pelas rotas em `app/Routes/Scraper/providers.py`.
2. O usuario pesquisa uma obra ou informa uma URL direta do provedor.
3. O backend resolve o provedor, carrega o projeto e monta a lista de capitulos.
4. Ao enfileirar capitulos, o backend grava jobs na tabela `scraper_downloads`.
5. O worker `scraper_downloads` reivindica jobs respeitando o limite de capitulos simultaneos por provedor.
6. O `app/Services/MD_Scrapper/runner.py` roteia o job para o runner correto.
7. O runner do provedor baixa as paginas/imagens e grava no diretorio configurado.

## Providers atuais

- `geass_comics`
- `manhastro`
- `hanami`

Os providers sao descobertos automaticamente pelo filesystem. Para uma pasta ser considerada um provider valido:

- ela precisa estar em `app/Services/MD_Scrapper/Providers/<provider_key>`
- precisa ser um diretorio
- nao pode comecar com `_` nem `.`
- precisa conter `provider.py`

Essa descoberta fica em `app/Services/md_scrapper_provider_fs.py`.

## Contrato de um provider

Cada provider precisa expor em `provider.py`:

```python
PROVIDER_DEFINITION = ScraperProviderDefinition(
    key="provider_key",
    label="Nome do Provider",
    search_projects=...,
    get_project=...,
    get_project_by_url=...,
    is_project_url=...,
    runner=...,
    min_app_version="1.3.0",
)
```

Campos esperados:

- `key`: nome da pasta do provider, em lowercase
- `label`: nome exibido na UI
- `search_projects(query)`: retorna lista de projetos normalizados
- `get_project(manga_id)`: retorna projeto + capitulos
- `get_project_by_url(url)`: mesma resposta de `get_project`
- `is_project_url(url)`: diz se a URL pertence ao provider
- `runner(job, hb)`: executa o download do job
- `min_app_version`: opcional; usado para bloquear providers incompativeis

### Formato esperado de um projeto

As buscas e detalhes do provider devem convergir para esse shape basico:

```python
{
    "id": "...",
    "provider": "manhastro",
    "title": "...",
    "original_title": "...",
    "description": "...",
    "cover_url": "https://...",
    "url": "https://...",
    "chapter_count": 123,
    "latest_chapter_at": "...",
}
```

Campos extras sao permitidos e podem ser uteis para UI ou debug.

### Formato esperado de um capitulo

```python
{
    "id": "...",
    "provider": "manhastro",
    "url": "https://...",
    "number": "85",
    "title": "Capitulo 85",
    "label": "Capitulo 85",
    "published_at": "...",
    "folder_name": "85",
}
```

Observacao:

- `folder_name` deve ser seguro para filesystem
- hoje a padronizacao preferida e usar apenas o numero do capitulo quando ele existir

### Retorno esperado de `get_project(...)`

```python
{
    "provider": "manhastro",
    "project": {...},
    "chapters": [{...}, {...}],
}
```

## Configuracao

Os defaults dos providers sao gerados em `app/Services/Database/settings_store.py` no escopo:

```text
md_scrapper_<provider_key>
```

Chaves atuais:

- `enabled` -> habilita ou bloqueia o provedor
- `chapters_concurrent` -> limite de capitulos simultaneos para aquele provedor
- `images_concurrent` -> limite interno de download de paginas/imagens dentro do runner do provedor
- `chapters_folder` -> pasta base de destino
- `max_retries` -> retries por job daquele provedor

Observacoes importantes:

- `chapters_concurrent` agora e respeitado por provedor no scheduler da fila `scraper_downloads`
- a quantidade total de threads do worker do MD Scrapper e a soma dos limites dos provedores
- `images_concurrent` continua sendo aplicado dentro do runner de cada provedor
- `max_retries` tambem e resolvido por provedor/job

## Jobs da fila

Os jobs criados em `scraper_downloads` carregam quatro blocos principais:

- `metadata`
- `files`
- `queue`
- `worker`

O campo mais importante para o roteamento e:

```python
job["metadata"]["provider"]
```

Se ele estiver ausente, o fallback usado e:

```python
job["metadata"]["source"]["provider"]
```

## Como adicionar um novo provider

1. Crie a pasta `app/Services/MD_Scrapper/Providers/<provider_key>`.
2. Adicione `provider.py`, `core.py`, `runner.py` e `__version__.py`.
3. Em `core.py`, implemente:
   - busca de projetos
   - resolucao de projeto por id/slug
   - resolucao de projeto por URL
   - normalizacao dos capitulos
   - manifesto do capitulo, se o runner precisar
4. Em `runner.py`, implemente o download real usando o `job` e o callback `hb`.
5. Em `provider.py`, exponha `PROVIDER_DEFINITION`.
6. Garanta que `key` no `PROVIDER_DEFINITION` seja exatamente o nome da pasta.
7. Se quiser exports diretos no modulo, atualize `app/Services/MD_Scrapper/__init__.py`.
8. Valide com:
   - busca real
   - detalhe real do projeto
   - manifesto real do capitulo
   - download real de pelo menos uma imagem
   - `python -m compileall app\\Services\\MD_Scrapper`

## Boas praticas para runners

- registrar eventos no record logger do job
- chamar `hb(...)` com frequencia para manter o lease vivo
- salvar manifestos/estado parcial quando fizer sentido
- usar `Referer`, cookies e headers corretos quando o site exigir
- tratar limites do provider com `images_concurrent` baixo quando necessario
- falhar com mensagens curtas e objetivas; isso vai parar em `last_error`

## Observacoes

- O modulo depende do plugin estar instalado e habilitado.
- Compatibilidade de versao do app e validada por provider via `min_app_version`.
- A fila do MD Scrapper e separada das filas normais de download/upload.
