from django.db import models


class Node(models.Model):
    name = models.CharField(max_length=50)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="children",
    )


class GraphNode(models.Model):
    name = models.CharField(max_length=50)


class Edge(models.Model):
    from_node = models.ForeignKey(
        GraphNode, on_delete=models.CASCADE, related_name="out_edges"
    )
    to_node = models.ForeignKey(
        GraphNode, on_delete=models.CASCADE, related_name="in_edges"
    )
