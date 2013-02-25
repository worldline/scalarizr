Feature: Cancel messages

Scenario: Cancel
    Given I have used the storage for 1000 MB
    And I have sent <create> message
    When I wait for 3 seconds
    And I send <cancel> message
    And I wait for 10 seconds
    Then I expect it canceled

Examples:
    | create                 | cancel                 |
    | DbMsr_CreateDataBundle | DbMsr_CancelDataBundle |
    | DbMsr_CreateBackup     | DbMsr_CancelBackup     |
