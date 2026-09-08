"""The MVP pipeline, one module per stage.

Read in pipeline order: config, store, extract, ingest, segment, retrieve, decide,
then the outputs (word_out, excel_out) and review. Each module is a stage; there is
no framework and no inheritance, so a reader can start anywhere and follow the data.
"""
