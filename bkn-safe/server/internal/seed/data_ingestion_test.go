// Copyright openbkn.ai
//
// Licensed under the OpenBKN License. See LICENSE-OPENBKN.txt in the project root.

package seed

import (
	"testing"

	"github.com/openbkn-ai/bkn-foundry/bkn-safe/server/internal/authz"
)

func TestDataIngestionCatalogAndNetworkBuilderGrant(t *testing.T) {
	db := newDB(t)
	enforcer, err := authz.New(db)
	if err != nil {
		t.Fatal(err)
	}
	if err := Apply(db, enforcer); err != nil {
		t.Fatal(err)
	}

	const networkBuilder = "1572fb82-526f-11f0-bde6-e674ec8dde71"
	const user = "data-ingestion-test-user"
	if err := enforcer.AssignRole(user, networkBuilder); err != nil {
		t.Fatal(err)
	}

	for _, operation := range []string{
		"view",
		"create",
		"modify",
		"delete",
		"task_manage",
		"agent_use",
		"mcp_execute",
	} {
		allowed, err := enforcer.Check(user, "data_ingestion", "platform", operation)
		if err != nil {
			t.Fatalf("check %s: %v", operation, err)
		}
		if !allowed {
			t.Errorf("network_builder should have data_ingestion:%s", operation)
		}
	}
}
